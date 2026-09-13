"""LLM-as-Judge 评测骨架（可选、轻量、默认离线）。

职责与诚实边界（对齐 AGENTS.md 第七条第 3 款与 docs/MODEL_EVALUATION.md）：
- Judge 只评估模糊语言质量（解释准确性 / 完整性 / 澄清清晰度 / 语气 / 引用一致性 /
  边界披露），**绝不**评估或替代确定性业务结论；
- 金额、权限、审批、状态迁移、幂等、副作用由确定性领域服务与规则引擎负责，
  Judge 分数不得改写业务失败（业务断言单独标记，见 deterministic_check）；
- 默认离线规则裁判（OfflineRuleJudge）：纯规则打分、确定性、零网络、零成本、零副作用；
- 真实裁判 LLMJudge 只有显式配置安全环境（OPSPILOT_LLM_API_KEY + 白名单
  OPSPILOT_LLM_BASE_URL，经 load_llm_settings + assert_safe_network 校验）才允许构造；
  构造失败（ModelConfigError）→ 调用方必须安全降级 OfflineRuleJudge 并 degraded=True；
- 输入先 PII 脱敏（复用 src/platform/redact.py），报告不得含完整手机号/邮箱/身份证；
- 本模块**不** import、不调用 src.domain.* / src.repo.* 的任何服务、适配器或写命令
  （Judge 零业务副作用）；也不在报告里声称"真实模型已验证 / 真实准确率 / 真实成本 / 真实延迟"。

真实裁判 LLMJudge 走既有 `OpenAICompatibleClient`（同一 Key/白名单门禁、同一内容守卫、
同一 PII 脱敏），因此真实调用路径**已实现且可用 mock 注入 `http_post` 单测**；但真实供应商
裁判**未实测**（本环境无安全 Key），报告一律标注"未实测"，除非确实发生了未降级的真实调用。
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.models.base import ModelConfigError  # noqa: E402
from src.models.config import (  # noqa: E402
    LLMSettings,
    assert_safe_network,
    is_allowed_base_url,
    load_llm_settings,
)
from src.models.openai_compatible import OpenAICompatibleClient  # noqa: E402
from src.platform.redact import redact_pii  # noqa: E402

DEFAULT_RUBRIC_PATH = ROOT / "evals" / "judge_rubric.json"
UNMEASURED_NOTE = "真实 Judge 未实测；当前结果为离线规则裁判；未产生网络请求。"
JUDGE_PROMPT_VERSION = "1.0"
MAX_REASON_CHARS = 200      # 只保留结构化理由摘要，禁止把模型完整推理（CoT）落库


# ---------- 异常 ----------

class RubricError(ValueError):
    """评测 Rubric 不合法（缺版本 / 维度为空 / 维度区间非法），安全失败，绝不静默用默认值。"""


# ---------- 数据模型 ----------

@dataclass(frozen=True)
class JudgeCase:
    """被评估的单条 Agent 输出/回复。

    - output_text：被评估的 Agent 输出/回复（judge_case 会先脱敏再交给裁判）；
    - expected_summary：可选，期望要点摘要（用于准确性/完整性参照）；
    - evidence_refs：可选，引用列表（用于引用一致性参照）；
    - business_ok / outcome / error_code：确定性业务结论（由领域服务/规则引擎给出，
      Judge 不得改写）。business_ok=None 表示未提供确定性结论。
    """
    case_id: str
    output_text: str
    expected_summary: Optional[str] = None
    evidence_refs: Optional[list[str]] = None
    business_ok: Optional[bool] = None
    outcome: Optional[str] = None
    error_code: Optional[str] = None


@dataclass(frozen=True)
class JudgeResult:
    """单条评测结果。

    - scores：维度 -> 分数（int，落在该维度 [min, max] 内）；
    - short_reason：只保留结构化理由摘要，**不得**保存模型内部完整推理/CoT；
    - degraded / error：是否降级与降级原因（异常类型名）；
    - measured：是否真实模型打分（当前恒 False）；
    - business_ok：透传确定性业务结论（None=未提供）；business_assertion_failed 由属性派生。
    """
    case_id: str
    rubric_version: str
    judge_model: str
    scores: dict[str, int]
    short_reason: str
    degraded: bool
    error: Optional[str]
    total_score: int
    max_score: int
    measured: bool = False
    business_ok: Optional[bool] = None

    @property
    def business_assertion_failed(self) -> bool:
        """确定性业务结论失败（Judge 分数不得把它表述为成功）。"""
        return self.business_ok is False


# ---------- Rubric 加载与校验 ----------

def load_rubric(path: Optional[Path | str] = None) -> dict:
    """加载并校验 rubric；任何不合法 → RubricError（安全失败，不用默认值兜底）。"""
    p = Path(path) if path is not None else DEFAULT_RUBRIC_PATH
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise RubricError(f"无法读取 rubric 文件 {p}：{e}") from e
    try:
        rubric = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RubricError(f"rubric 文件不是合法 JSON：{e}") from e
    _validate_rubric(rubric)
    return rubric


def _validate_rubric(rubric: dict) -> None:
    if not isinstance(rubric, dict):
        raise RubricError("rubric 顶层必须是 JSON object")
    version = rubric.get("rubric_version")
    if not isinstance(version, str) or not version.strip():
        raise RubricError("rubric 缺少非空 rubric_version 字段")
    dims = rubric.get("dimensions")
    if not isinstance(dims, dict) or not dims:
        raise RubricError("rubric 缺少非空 dimensions 字段")
    for name, spec in dims.items():
        if not isinstance(spec, dict):
            raise RubricError(f"维度 {name!r} 的配置必须是 object")
        lo = spec.get("min")
        hi = spec.get("max")
        if type(lo) is not int or type(hi) is not int:
            raise RubricError(f"维度 {name!r} 的 min/max 必须是整数（收到 {lo!r}/{hi!r}）")
        if lo >= hi:
            raise RubricError(f"维度 {name!r} 的 min({lo}) 必须小于 max({hi})")


# ---------- 离线规则裁判（默认） ----------

# 澄清问句/礼貌/边界等确定性关键词（只衡量语言质量，不做业务裁决）
_CLARIFY_WORDS = ("请", "请问", "确认", "什么", "哪", "多少", "如何", "是否", "麻烦", "提供")
_POLITE_WORDS = ("您", "您好", "请", "抱歉", "感谢", "建议", "我们", "核实", "耐心")
_UNPROFESSIONAL_WORDS = ("垃圾", "滚", "傻", "脑残", "投诉你", "骂")
_BOUNDARY_WORDS = (
    "系统裁决", "由系统", "由确定性", "金额以系统", "审批由", "权限由",
    "不做出审批", "不产出金额", "转人工", "人工处理", "系统决定", "以系统为准",
    "不迁移状态",
)
_KNOWN_DIMENSIONS = {
    "accuracy", "completeness", "clarity", "tone", "citation_alignment", "boundary_disclosure",
}


class OfflineRuleJudge:
    """默认裁判：纯规则打分（不联网、零成本、零副作用、确定性）。

    规则只衡量语言质量：
    - 含引用且与 evidence_refs 一致 → citation_alignment 高分；
    - 含"金额/审批/权限由系统裁决、转人工"等边界语 → boundary_disclosure 高分；
    - 含澄清问句 → clarity 高分；
    - 越短越平淡 → tone 中性（不夸大为优秀）。
    未知维度保守给最低分（不臆造）。
    """

    name = "offline/rule-judge-v1"
    model_version = "1.0"
    measured = False

    def judge(self, case: JudgeCase, rubric: dict) -> JudgeResult:
        text = case.output_text or ""
        dims = rubric["dimensions"]
        scores: dict[str, int] = {}
        reasons: list[str] = []
        for dim_name, spec in dims.items():
            lo, hi = int(spec["min"]), int(spec["max"])
            score, reason = self._score_dimension(dim_name, text, case, lo, hi)
            scores[dim_name] = score
            reasons.append(f"{dim_name}={score}({reason})")
        total = sum(scores.values())
        max_score = sum(int(s["max"]) for s in dims.values())
        return JudgeResult(
            case_id=case.case_id,
            rubric_version=rubric["rubric_version"],
            judge_model=self.name,
            scores=scores,
            short_reason="离线规则：" + "；".join(reasons),
            degraded=False,
            error=None,
            total_score=total,
            max_score=max_score,
            measured=self.measured,
            business_ok=case.business_ok,
        )

    def _score_dimension(self, dim_name: str, text: str, case: JudgeCase, lo: int, hi: int):
        if dim_name == "accuracy":
            return self._accuracy(text, case, lo, hi)
        if dim_name == "completeness":
            return self._completeness(text, case, lo, hi)
        if dim_name == "clarity":
            return self._clarity(text, lo, hi)
        if dim_name == "tone":
            return self._tone(text, lo, hi)
        if dim_name == "citation_alignment":
            return self._citation(text, case, lo, hi)
        if dim_name == "boundary_disclosure":
            return self._boundary(text, lo, hi)
        return lo, "未知维度（保守给最低分）"

    @staticmethod
    def _scale(ratio: float, lo: int, hi: int) -> int:
        ratio = max(0.0, min(1.0, ratio))
        return lo + round(ratio * (hi - lo))

    @staticmethod
    def _split_points(summary: Optional[str]) -> list[str]:
        if not summary:
            return []
        return [seg.strip() for seg in re.split(r"[；;。，,\n]", summary) if seg.strip()]

    def _accuracy(self, text: str, case: JudgeCase, lo: int, hi: int):
        points = self._split_points(case.expected_summary)
        if not points:
            return lo, "无期望要点（准确性未核验，给最低分）"
        hits = sum(1 for p in points if p in text)
        return self._scale(hits / len(points), lo, hi), f"命中{hits}/{len(points)}要点"

    def _completeness(self, text: str, case: JudgeCase, lo: int, hi: int):
        points = self._split_points(case.expected_summary)
        if points:
            hits = sum(1 for p in points if p in text)
            return self._scale(hits / len(points), lo, hi), f"覆盖{hits}/{len(points)}要点"
        n = len(text.strip())
        if n == 0:
            return lo, "空输出"
        if n < 12:
            return lo, "过短"
        if n < 40:
            return (lo + hi) // 2, "简短"
        return hi, "信息较充分"

    def _clarity(self, text: str, lo: int, hi: int):
        has_q = ("？" in text) or ("?" in text)
        has_kw = any(w in text for w in _CLARIFY_WORDS)
        if has_q and has_kw:
            return hi, "含澄清问句"
        if has_q or has_kw:
            return (lo + hi) // 2, "澄清意图不完整"
        return lo, "无澄清问句"

    def _tone(self, text: str, lo: int, hi: int):
        if any(w in text for w in _UNPROFESSIONAL_WORDS):
            return lo, "含不礼貌用词"
        polite = sum(1 for w in _POLITE_WORDS if w in text)
        if polite >= 2:
            return hi, "礼貌标记充分"
        if polite == 1:
            return (lo + hi) // 2, "礼貌标记一般"
        return (lo + hi) // 2, "平淡（中性）"

    def _citation(self, text: str, case: JudgeCase, lo: int, hi: int):
        refs = case.evidence_refs or []
        if not refs:
            return (lo + hi) // 2, "未提供引用基线（中性）"
        hits = sum(1 for r in refs if r and r in text)
        if hits == len(refs):
            return hi, f"引用一致{hits}/{len(refs)}"
        if hits > 0:
            return (lo + hi) // 2, f"引用部分一致{hits}/{len(refs)}"
        return lo, "输出未含引用"

    def _boundary(self, text: str, lo: int, hi: int):
        if any(w in text for w in _BOUNDARY_WORDS):
            return hi, "含边界说明"
        return lo, "未说明边界"


# ---------- 真实 LLM 裁判（可选；真实供应商未实测，路径可用 mock 单测） ----------

_JUDGE_SAFETY_RULES = """
评分纪律（必须遵守）：
1. 只评估语言表达质量，不评估业务正确性；
2. short_reason 只写一句话结论摘要（<=100 字），禁止输出推理过程/思维链；
3. 禁止在 short_reason 中出现金额数字、以及「批准 / 拒绝 / 执行 / 关闭工单 / 状态迁移」
   等业务动作词——这些结论不属于 Judge 的职责，出现会被内容守卫拦截并导致本次评测降级。
""".strip()


def build_judge_prompt(rubric: dict) -> str:
    """构造版本化裁判 Prompt（维度与分数区间来自 rubric，不硬编码）。"""
    dims = rubric["dimensions"]
    dim_lines = [
        f"- {name}：{spec.get('description', '')}（{int(spec['min'])}-{int(spec['max'])} 分）"
        for name, spec in dims.items()
    ]
    out_of_scope = "、".join(rubric.get("out_of_scope") or [])
    return (
        f"你是售后 Agent 回复质量评测员（Judge prompt 版本 {JUDGE_PROMPT_VERSION}，"
        f"rubric 版本 {rubric['rubric_version']}）。\n"
        "你会收到【待评估输出】【期望要点】【应引用的证据】三段文本，请只对语言质量打分。\n\n"
        "评分维度：\n" + "\n".join(dim_lines) + "\n\n"
        f"明确不属于你的评估范围（不要评分、不要据此加减分）：{out_of_scope}。\n\n"
        + _JUDGE_SAFETY_RULES + "\n\n"
        "只输出 JSON：各维度整数分数 + 字段 short_reason（一句话摘要）。"
    )


def build_judge_schema(rubric: dict):
    """按 rubric 维度动态构造输出 Schema：分数区间由 rubric 约束（越界即 Schema 错误 → 降级）。"""
    from pydantic import Field, create_model

    fields: dict = {}
    for name, spec in rubric["dimensions"].items():
        fields[name] = (int, Field(..., ge=int(spec["min"]), le=int(spec["max"])))
    fields["short_reason"] = (str, Field(..., max_length=MAX_REASON_CHARS))
    return create_model(f"JudgeScores_{str(rubric['rubric_version']).replace('.', '_')}", **fields)


def render_judge_input(case: "JudgeCase") -> str:
    """裁判输入：显式分段，便于模型定位；调用前已脱敏。"""
    refs = "、".join(case.evidence_refs or []) or "（未提供）"
    return (
        f"【待评估输出】\n{case.output_text}\n\n"
        f"【期望要点】\n{case.expected_summary or '（未提供）'}\n\n"
        f"【应引用的证据】\n{refs}"
    )


class LLMJudge:
    """真实 LLM 裁判（可选；真实供应商未实测）。

    只有显式配置安全环境（OPSPILOT_LLM_API_KEY + 白名单 OPSPILOT_LLM_BASE_URL）时才允许
    构造；构造失败抛 ModelConfigError，调用方必须安全降级到 OfflineRuleJudge。
    调用走 OpenAICompatibleClient（同一内容守卫 + PII 脱敏 + 零副作用），http_post 可注入
    以便用 mock 单测整条路径；任何失败（超时/Schema/内容守卫）由 judge_case 降级为离线规则
    并标记 degraded=True，绝不把失败或未执行的调用表述为实测。
    """

    provider = "openai-compatible-judge"
    measured = True

    def __init__(self, settings: Optional[LLMSettings] = None, http_post=None):
        settings = settings if settings is not None else load_llm_settings()
        assert_safe_network(settings)  # 缺 Key / URL 非白名单 → ModelConfigError（零网络）
        self._settings = settings
        self._client = OpenAICompatibleClient(settings, http_post=http_post)
        self.name = f"{self.provider}/{settings.model}"
        self.model_version = settings.model_version
        self.prompt_version = JUDGE_PROMPT_VERSION
        self.calls = 0               # 真实调用尝试次数（用于报告的 network_requests）

    def judge(self, case: JudgeCase, rubric: dict) -> JudgeResult:
        schema = build_judge_schema(rubric)
        prompt = build_judge_prompt(rubric)
        self.calls += 1
        resp = self._client.invoke("llm_judge", prompt, schema,
                                   {"text": render_judge_input(case)},
                                   dataset_version=str(rubric["rubric_version"]))
        payload = resp.payload
        dims = rubric["dimensions"]
        scores = {name: int(getattr(payload, name)) for name in dims}
        reason = " ".join(str(payload.short_reason).split())[:MAX_REASON_CHARS]
        return JudgeResult(
            case_id=case.case_id,
            rubric_version=rubric["rubric_version"],
            judge_model=self.name,
            scores=scores,
            short_reason=f"模型裁判（{self.prompt_version}）：{reason}",
            degraded=False,
            error=None,
            total_score=sum(scores.values()),
            max_score=sum(int(s["max"]) for s in dims.values()),
            measured=True,
            business_ok=case.business_ok,
        )


# ---------- 统一入口 ----------

def _redact_case(case: JudgeCase) -> JudgeCase:
    """先脱敏再交给裁判：输出文本 / 期望摘要 / 引用逐项脱敏（业务标记原样透传）。"""
    refs = [redact_pii(r) for r in case.evidence_refs] if case.evidence_refs else None
    return JudgeCase(
        case_id=case.case_id,
        output_text=redact_pii(case.output_text or ""),
        expected_summary=redact_pii(case.expected_summary) if case.expected_summary else None,
        evidence_refs=refs,
        business_ok=case.business_ok,
        outcome=case.outcome,
        error_code=case.error_code,
    )


def judge_case(case: JudgeCase, rubric: dict, judge=None) -> JudgeResult:
    """统一评测入口：先脱敏 → 调用裁判；裁判抛异常 → 降级 OfflineRuleJudge（不抛出）。

    judge 可为任意实现 judge(case, rubric) -> JudgeResult 的对象；默认 OfflineRuleJudge。
    """
    judge = judge if judge is not None else OfflineRuleJudge()
    safe_case = _redact_case(case)
    try:
        return judge.judge(safe_case, rubric)
    except Exception as e:  # noqa: BLE001  裁判异常一律安全降级，不外抛
        fallback = OfflineRuleJudge().judge(safe_case, rubric)
        return replace(fallback, degraded=True, error=type(e).__name__, measured=False)


# ---------- 裁判构造（诚实标记 measured / network_requests / blockers） ----------

def build_judge(mode: str):
    """构造裁判：offline 恒离线；judge 满足全部安全条件才启用真实裁判，否则安全降级。

    返回 (judge, notes, state)。state 含 measured / network_requests / blockers 等诚实标记。
    `measured=True` 只表示"真实裁判已启用、允许联网"；是否真的实测由 run_judge 依据
    **未降级的实际调用**判定（全部降级则报告仍写未实测）。
    """
    offline_state = {
        "judge_model": OfflineRuleJudge.name,
        "model_version": OfflineRuleJudge.model_version,
        "prompt_version": "N/A（规则裁判）",
        "measured": False,
        "network_requests": 0,
        "blockers": [],
    }
    if mode == "offline":
        return OfflineRuleJudge(), [
            "离线规则裁判（不联网、零成本、确定性打分；"
            "不评估金额/权限/审批/状态/幂等/副作用）",
        ], offline_state

    settings = load_llm_settings()
    blockers: list[str] = []
    if not (settings.api_key or "").strip():
        blockers.append("未配置 OPSPILOT_LLM_API_KEY")
    if not is_allowed_base_url(settings.base_url, settings.allowed_base_urls):
        blockers.append(f"OPSPILOT_LLM_BASE_URL 不在白名单（{settings.base_url!r}）")
    if not settings.model_configured:
        blockers.append("未显式配置模型名（OPSPILOT_LLM_MODEL）")
    if blockers:
        return OfflineRuleJudge(), [
            f"真实 Judge 未启用（安全失败，未联网）：{'；'.join(blockers)}。",
            UNMEASURED_NOTE,
        ], {**offline_state, "blockers": blockers}

    try:
        judge = LLMJudge(settings)
    except ModelConfigError as e:      # 兜底：构造期安全校验失败同样不联网
        return OfflineRuleJudge(), [
            f"真实 Judge 未启用（构造期安全校验失败，未联网）：{e}", UNMEASURED_NOTE,
        ], {**offline_state, "blockers": [str(e)]}

    return judge, [
        f"真实裁判已启用：{settings.model}（provider={LLMJudge.provider}，"
        f"prompt={JUDGE_PROMPT_VERSION}）；{settings.redacted_summary()}。",
        "该次运行是否算“实测”取决于是否存在未降级的真实调用；"
        "任何超时/Schema/内容守卫失败都会降级为离线规则并如实标记 degraded。",
    ], {
        "judge_model": judge.name,
        "model_version": settings.model_version,
        "prompt_version": JUDGE_PROMPT_VERSION,
        "measured": True,
        "network_requests": 0,          # 实际调用次数在 run_judge 中统计
        "blockers": [],
    }


# ---------- 批量执行与报告 ----------

def run_judge(cases, mode: str = "offline", outdir: Optional[Path] = None) -> dict:
    """批量执行评测并返回结构化结果。mode: offline | judge。

    返回 dict 含 rubric_version / judge_model / mode / measured / results / aggregate /
    degraded_count / deterministic_check / business_assertion_failed / report_path 等字段。
    """
    rubric = load_rubric()
    judge, notes, state = build_judge(mode)
    results = [judge_case(case, rubric, judge=judge) for case in cases]

    total = len(results)
    degraded = [r for r in results if r.degraded]
    # 实测判定：真实裁判已启用 **且** 至少有一条未降级的真实调用结果；
    # 全部降级（超时/Schema/内容守卫）→ 仍按未实测报告，绝不夸大。
    measured_cases = [r for r in results if r.measured]
    measured = bool(state["measured"]) and bool(measured_cases)
    network_requests = int(getattr(judge, "calls", 0)) if state["measured"] else 0
    dims = list(rubric["dimensions"].keys())
    dimension_mean: dict[str, Optional[float]] = {}
    dimension_min: dict[str, Optional[int]] = {}
    for dim in dims:
        vals = [r.scores[dim] for r in results if r.scores.get(dim) is not None]
        dimension_mean[dim] = round(statistics.mean(vals), 2) if vals else None
        dimension_min[dim] = min(vals) if vals else None
    totals = [r.total_score for r in results]
    maxs = [r.max_score for r in results]
    business_failed = [r.case_id for r in results if r.business_assertion_failed]
    checked = sum(1 for r in results if r.business_ok is not None)

    result = {
        "mode": mode,
        "rubric_version": rubric["rubric_version"],
        "judge_model": state["judge_model"],
        "model_version": state["model_version"],
        "prompt_version": state.get("prompt_version", JUDGE_PROMPT_VERSION),
        "measured": measured,
        "judge_enabled": bool(state["measured"]),
        "measured_cases": len(measured_cases),
        "network_requests": network_requests,
        "blockers": state.get("blockers", []),
        "total": total,
        "results": [asdict(r) for r in results],
        "aggregate": {
            "dimension_mean": dimension_mean,
            "dimension_min": dimension_min,
            "avg_total_score": round(statistics.mean(totals), 2) if totals else None,
            "max_score": maxs[0] if maxs else None,
        },
        "degraded_count": len(degraded),
        "deterministic_check": {
            "checked": checked,
            "business_assertion_failed_count": len(business_failed),
            "business_assertion_failed": business_failed,
            "note": "Judge 分数不能替代业务断言（金额/权限/审批/状态/幂等/副作用由确定性检查负责）",
        },
        "business_assertion_failed": business_failed,
        "notes": notes,
        "report_path": None,
    }

    out = _report_dir(mode, measured, outdir)
    out.mkdir(parents=True, exist_ok=True)
    report_name = "judge_offline.md" if mode == "offline" else "judge_candidate.md"
    report_path = out / report_name
    report_path.write_text(_render(result), encoding="utf-8")
    result["report_path"] = str(report_path)
    return result


def _report_dir(mode: str, measured: bool, outdir: Optional[Path]) -> Path:
    """报告落盘目录（防止把离线降级写成真实 Judge 成绩）。

    - offline：canonical 报告 → `evals/reports/`；
    - judge 且**存在未降级的真实调用**：→ `evals/reports/`；
    - judge 但未实测（无安全 Key / 全部降级）：→ `.runtime/reports/`
      （D 盘运行时目录，不进入版本库），避免误导性的
      `judge_candidate.md` 被当成真实模型评测结果。
    """
    if outdir is not None:
        return Path(outdir)
    if mode == "offline" or measured:
        return ROOT / "evals" / "reports"
    from src.platform.runtime_paths import runtime_dir
    return runtime_dir("reports")


def _render(r: dict) -> str:
    measured = r["measured"]
    lines = [
        "# LLM-as-Judge 评测报告",
        "",
        "## 运行标识",
        "",
        f"- 裁判模型：`{r['judge_model']}`",
        f"- 裁判版本：`{r['model_version']}`",
        f"- 裁判 Prompt 版本：`{r['prompt_version']}`",
        f"- Rubric 版本：`{r['rubric_version']}`",
        f"- 运行模式：`{r['mode']}`",
        f"- 实测状态：{'实测（存在未降级的真实模型裁判结果）' if measured else '未实测（离线规则裁判）'}",
        f"- 真实裁判启用：{r['judge_enabled']}；未降级实测条数：{r['measured_cases']}",
        f"- 网络请求数：{r['network_requests']}",
        f"- 样本数：{r['total']}",
        f"- 降级数：{r['degraded_count']}",
        "",
        "## 聚合分数",
        "",
    ]
    agg = r["aggregate"]
    if agg["dimension_mean"]:
        lines.append("- 各维度均分：" + "；".join(
            f"{k}={v}" for k, v in agg["dimension_mean"].items()))
        lines.append("- 各维度最低分：" + "；".join(
            f"{k}={v}" for k, v in agg["dimension_min"].items()))
    lines.append(f"- 平均总分：{agg['avg_total_score']} / {agg['max_score']}")
    lines += [
        "",
        "## 确定性业务结论（Judge 不替代）",
        "",
    ]
    dc = r["deterministic_check"]
    lines.append(f"- 已提供确定性结论：{dc['checked']} 条")
    failed = dc["business_assertion_failed"]
    lines.append(f"- 业务断言失败（business_ok=False）：{dc['business_assertion_failed_count']} 条"
                 + (f"（{', '.join(failed)}）" if failed else ""))
    lines.append(f"- {dc['note']}。")
    lines.append("- 本报告不运行确定性业务验收（由 evals/replay.py 负责）；Judge 分数不改变确定性业务结论。")
    lines += [
        "",
        "## 逐条结果",
        "",
        "| case_id | rubric_version | judge_model | scores | total/max | short_reason | degraded | error | business_ok |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in r["results"]:
        scores_str = " ".join(f"{k}={v}" for k, v in row["scores"].items())
        biz = "-" if row["business_ok"] is None else ("失败" if row["business_ok"] is False else "通过")
        lines.append(
            f"| {row['case_id']} | {row['rubric_version']} | {row['judge_model']} | "
            f"{scores_str} | {row['total_score']}/{row['max_score']} | "
            f"{row['short_reason']} | {row['degraded']} | {row['error'] or '-'} | {biz} |"
        )
    lines += [
        "",
        "## 说明",
        "",
    ]
    lines += [f"- {n}" for n in r["notes"]]
    if r["blockers"]:
        lines.append(f"- 未启用真实裁判的原因：{'；'.join(r['blockers'])}。")
    if not measured:
        lines.append(f"- {UNMEASURED_NOTE}")
        lines.append("- 不得将本报告中的任何分数表述为真实模型的准确率、成本或延迟。")
    lines += [
        "",
        "## 声明（不可省略）",
        "",
        "- Judge 不能替代业务断言（金额/权限/审批/状态/幂等/副作用由确定性检查负责）；",
        "- Judge 分数未经人工校准时只能作为参考；",
        "- 没有真实模型时属于离线/未实测。",
        "",
        "> 诚实边界：本报告不含 API Key、完整请求体或客户 PII；真实 Judge 必须实际运行并保存报告后"
        "才能声称已实测，未运行一律标注未实测。Judge 分数不改变确定性业务结论。",
    ]
    return "\n".join(lines)
