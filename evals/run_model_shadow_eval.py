"""阶段 5A 影子评测入口：模型预测与离线规则 / 黄金集对照（含 token 与成本记账）。

用法（项目根，D 盘 .venv）：
    .venv\\Scripts\\python.exe evals\\run_model_shadow_eval.py --mode offline
    .venv\\Scripts\\python.exe evals\\run_model_shadow_eval.py --mode candidate

影子原则：
- 正常业务继续使用离线规则；候选模型只对同一输入做预测；
- 预测不触碰领域服务 / 数据库 / 审批（零业务副作用）；
- `--mode candidate` 只有在**全部**安全条件满足时才联网：
    1) `OPSPILOT_LLM_API_KEY` 存在；
    2) `OPSPILOT_LLM_BASE_URL` 在白名单内；
    3) 模型名显式配置（`OPSPILOT_LLM_MODEL`）；
  价格未配置不阻断联网，但报告必须把成本标为 N/A（不伪造成本）。
  任一条件不满足 → 安全降级为离线规则对照，**零网络请求**，报告明确写"未实测"。

报告：evals/reports/shadow_eval_<mode>.md（候选模型/模型版本/Prompt 版本/数据集版本/运行时间/
运行模式/总样本数/意图准确率/澄清识别/结构化输出错误数/内容安全拦截数/降级次数/
输入 token/输出 token/估算成本/P50-P95 延迟/业务副作用 0/价格配置来源）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.models import (
    IntentExtraction,
    ModelConfigError,
    ModelContentPolicyError,
    ModelError,
    ModelGateway,
    ModelInvocationMetadata,
    ModelParseError,
    ModelSchemaError,
    is_allowed_base_url,
    load_llm_settings,
)

DATASET_VERSION = "golden-v1"
PROMPT_VERSION = "1.0"
UNMEASURED_NOTE = "候选模型未实测；当前结果为离线规则对照；未产生网络请求。"

# 分类计数（结构化输出 vs 内容安全）
_SCHEMA_ERRORS = {"ModelSchemaError", "ModelParseError"}
_POLICY_ERRORS = {"ModelContentPolicyError"}


def load_cases() -> list[dict]:
    file = ROOT / "evals" / "golden" / "golden_v1.json"
    return json.loads(file.read_text(encoding="utf-8"))


def _prediction_to_dict(payload: IntentExtraction) -> dict:
    return {
        "intent": payload.intent.value,
        "order_id": payload.order_id,
        "reason_tags": list(payload.reason_tags),
        "missing_fields": list(payload.missing_fields),
        "confidence": payload.confidence,
    }


def _sum_optional(values: list[Optional[float]]) -> Optional[float]:
    """仅在**每一行**都有值时求和；否则 None（未配置价格 → N/A，不用 0 冒充）。"""
    if not values or any(v is None for v in values):
        return None
    return round(sum(values), 6)


def _money(value: Optional[float], currency: str = "USD") -> str:
    return "N/A" if value is None else f"{currency} {value:.6f}"


def run(mode: str, limit: Optional[int] = None, outdir: Optional[Path] = None) -> dict:
    """执行影子评测并返回结构化结果。mode: offline | candidate。"""
    cases = load_cases()
    if limit:
        cases = cases[:limit]
    started = time.monotonic()

    gateway, notes, candidate_state = _build_gateway(mode)
    rows: list[dict] = []
    for case in cases:
        text = case["request"]
        try:
            resp = gateway.analyze_intent(text, dataset_version=DATASET_VERSION)
            payload, meta = resp.payload, resp.metadata
            pred = _prediction_to_dict(payload)
            error = meta.error
        except ModelError as e:
            pred = {}
            meta = ModelInvocationMetadata(
                provider="error", model_name="error", task="intent_classification",
                prompt_version=PROMPT_VERSION, duration_ms=0.0, degraded=True,
                error=type(e).__name__, token_source="not_applicable",
            )
            error = type(e).__name__

        expected_intent = case.get("expected", {}).get("intent")
        intent_ok = (expected_intent is None) or (pred.get("intent") == expected_intent)
        # 澄清识别：该输入在本次运行下是否需要澄清（缺参非空）
        needs_clarify = bool(pred.get("missing_fields"))
        rows.append({
            "case_id": case["id"], "scenario": case.get("scenario", ""),
            "provider": meta.provider, "model": meta.model_name,
            "degraded": meta.degraded, "error": error,
            "prediction": pred, "expected_intent": expected_intent,
            "intent_ok": intent_ok, "needs_clarify": needs_clarify,
            "duration_ms": meta.duration_ms, "input_tokens": meta.input_tokens,
            "output_tokens": meta.output_tokens,
            "input_cost": meta.input_cost, "output_cost": meta.output_cost,
            "cost_usd": meta.cost_estimate_usd, "currency": meta.currency,
            "pricing_source": meta.pricing_source, "token_source": meta.token_source,
        })

    total = len(rows)
    intent_ok_n = sum(1 for r in rows if r["expected_intent"] is not None and r["intent_ok"])
    intent_denom = sum(1 for r in rows if r["expected_intent"] is not None)
    errors = [r for r in rows if r["error"]]
    degraded = [r for r in rows if r["degraded"]]
    schema_errors = [r for r in rows if r["error"] in _SCHEMA_ERRORS]
    policy_blocks = [r for r in rows if r["error"] in _POLICY_ERRORS]
    durations = sorted(r["duration_ms"] for r in rows)
    input_tokens = sum(r["input_tokens"] for r in rows)
    output_tokens = sum(r["output_tokens"] for r in rows)
    input_cost = _sum_optional([r["input_cost"] for r in rows])
    output_cost = _sum_optional([r["output_cost"] for r in rows])
    total_cost = _sum_optional([r["cost_usd"] for r in rows])
    currency = rows[0]["currency"] if rows else "USD"
    avg_cost = (None if total_cost is None or not total
                else round(total_cost / total, 6))
    elapsed = time.monotonic() - started

    result = {
        "mode": mode,
        "dataset_version": DATASET_VERSION,
        "prompt_version": PROMPT_VERSION,
        "model_name": candidate_state["model_name"],
        "model_version": candidate_state["model_version"],
        "provider": candidate_state["provider"],
        "candidate_measured": candidate_state["measured"],
        "candidate_blockers": candidate_state["blockers"],
        "network_requests": candidate_state["network_requests"],
        "elapsed_seconds": round(elapsed, 3),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": total,
        "intent_accuracy": round(intent_ok_n / intent_denom, 4) if intent_denom else None,
        "clarify_detected": sum(1 for r in rows if r["needs_clarify"]),
        "clarify_cases": [r["case_id"] for r in rows if r["needs_clarify"]],
        "error_count": len(errors),
        "error_rate": round(len(errors) / total, 4) if total else 0.0,
        "schema_error_count": len(schema_errors),
        "policy_block_count": len(policy_blocks),
        "degraded_count": len(degraded),
        "p50_ms": _percentile(durations, 50), "p95_ms": _percentile(durations, 95),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "total_cost": total_cost,
        "avg_cost_per_case": avg_cost,
        "currency": currency,
        "pricing_source": candidate_state["pricing_source"],
        "pricing_configured": candidate_state["pricing_configured"],
        "notes": notes,
        "business_side_effect": "0（影子模式仅文本预测，未调用任何领域写路径）",
        "rows": rows,
    }

    out = _report_dir(mode, candidate_state["measured"], outdir)
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / f"shadow_eval_{mode}.md"
    report_path.write_text(_render(result), encoding="utf-8")
    result["report_path"] = str(report_path)

    print(_summary(result))
    return result


def _report_dir(mode: str, measured: bool, outdir: Optional[Path]) -> Path:
    """报告落盘目录（防止把离线降级写成真实候选模型成绩）。

    - offline：canonical 报告 → `evals/reports/`（可提交、可复现）；
    - candidate 且**真实调用成功**：→ `evals/reports/`；
    - candidate 但未实测（无安全 Key / 未显式配置模型名 → 安全降级）：
      → `.runtime/reports/`（D 盘运行时目录，不进入版本库），
      避免误导性的 `shadow_eval_candidate.md` 被当成真实候选模型结果。
    """
    if outdir is not None:
        return Path(outdir)
    if mode == "offline" or measured:
        return ROOT / "evals" / "reports"
    from src.platform.runtime_paths import runtime_dir
    return runtime_dir("reports")


def _build_gateway(mode: str):
    """构造评测网关：offline 恒不联网；candidate 满足全部安全条件才联网。"""
    offline_state = {
        "provider": "offline-rule", "model_name": "offline/rules-v1",
        "model_version": "1.0", "measured": False, "blockers": [],
        "network_requests": 0, "pricing_source": "N/A（离线规则不调用模型，无 token 计费）",
        "pricing_configured": False,
    }
    if mode == "offline":
        return (ModelGateway(),
                ["离线规则基线（不联网、零成本：tokens=0，cost=N/A，provider=offline-rule）"],
                offline_state)

    settings = load_llm_settings()
    blockers: list[str] = []
    if not (settings.api_key or "").strip():
        blockers.append("未配置 OPSPILOT_LLM_API_KEY")
    if not is_allowed_base_url(settings.base_url, settings.allowed_base_urls):
        blockers.append(f"OPSPILOT_LLM_BASE_URL 不在白名单（{settings.base_url!r}）")
    if not settings.model_configured:
        blockers.append("未显式配置模型名（OPSPILOT_LLM_MODEL）")
    if settings.price_errors:
        blockers_price_hint = ("；".join(settings.price_errors) + " 取值非法，已安全回退为未配置")
    else:
        blockers_price_hint = ""

    pricing_note = (f"价格配置来源：{settings.pricing_source}"
                    + (f"（{blockers_price_hint}）" if blockers_price_hint else ""))
    if not settings.pricing_configured:
        pricing_note += "；成本标记为 N/A（未配置完整单价）"

    if blockers:
        return (ModelGateway(),
                [f"candidate 主模型未启用（安全失败，未联网）：{'；'.join(blockers)}。",
                 UNMEASURED_NOTE,
                 pricing_note],
                {**offline_state, "blockers": blockers,
                 "pricing_source": settings.pricing_source,
                 "pricing_configured": settings.pricing_configured})

    try:
        gateway = ModelGateway(settings=settings)
    except ModelConfigError as e:      # 兜底：构造期安全校验失败同样不联网
        return (ModelGateway(),
                [f"candidate 主模型未启用（构造期安全校验失败，未联网）：{e}", UNMEASURED_NOTE,
                 pricing_note],
                {**offline_state, "blockers": [str(e)],
                 "pricing_source": settings.pricing_source,
                 "pricing_configured": settings.pricing_configured})

    return (gateway,
            [f"主模型已启用：{settings.model}（provider=openai-compatible）；"
             f"base={settings.redacted_summary()}。",
             pricing_note],
            {"provider": "openai-compatible", "model_name": settings.model,
             "model_version": settings.model_version, "measured": True, "blockers": [],
             "network_requests": len(load_cases()),
             "pricing_source": settings.pricing_source,
             "pricing_configured": settings.pricing_configured})


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(p / 100 * len(sorted_values)))
    return sorted_values[idx]


def _summary(r: dict) -> str:
    marker = "" if r["candidate_measured"] else (
        "（未实测）" if r["mode"] == "candidate" else "")
    cost = _money(r["total_cost"], r["currency"]) if r["total_cost"] is not None else "N/A"
    return (
        f"影子评测[{r['mode']}]{marker}：模型={r['model_name']}@{r['model_version']} "
        f"dataset={r['dataset_version']} prompt={r['prompt_version']} "
        f"意图准确率={r['intent_accuracy']} 错误率={r['error_rate']} "
        f"结构化错误={r['schema_error_count']} 安全拦截={r['policy_block_count']} "
        f"降级数={r['degraded_count']} P95={r['p95_ms']}ms "
        f"tokens(in/out/total)={r['input_tokens']}/{r['output_tokens']}/{r['total_tokens']} "
        f"cost(in/out/total)={_money(r['input_cost'], r['currency'])}/"
        f"{_money(r['output_cost'], r['currency'])}/{cost} "
        f"avg={_money(r['avg_cost_per_case'], r['currency'])} "
        f"价格来源={r['pricing_source']} 报告={r['report_path']}"
    )


def _render(r: dict) -> str:
    measured = r["candidate_measured"]
    lines = [
        "# 影子评测报告（售后意图与澄清识别）",
        "",
        "## 运行标识",
        "",
        f"- 候选模型：`{r['model_name']}`",
        f"- 模型版本：`{r['model_version']}`",
        f"- Prompt 版本：`{r['prompt_version']}`",
        f"- 数据集版本：`{r['dataset_version']}`",
        f"- 运行时间：{r['started_at']}（耗时 {r['elapsed_seconds']} s）",
        f"- 运行模式：`{r['mode']}`（provider `{r['provider']}`）",
        f"- 网络请求数：{r['network_requests']}"
        + ("（候选模型实测）" if measured else "（未联网）"),
        "",
        "## 指标",
        "",
        f"- 总样本数：{r['total']}",
        f"- 意图准确率：{r['intent_accuracy']}",
        f"- 澄清识别结果：{r['clarify_detected']} 条需澄清"
        + (f"（{', '.join(r['clarify_cases'])}）" if r["clarify_cases"] else ""),
        f"- 结构化输出错误数：{r['schema_error_count']}",
        f"- 内容安全拦截数：{r['policy_block_count']}",
        f"- 降级次数：{r['degraded_count']}",
        f"- 其他错误数：{r['error_count']}（错误率 {r['error_rate']}）",
        f"- P50 延迟：{r['p50_ms']} ms｜P95 延迟：{r['p95_ms']} ms",
        f"- 业务副作用：{r['business_side_effect']}",
        "",
        "## Token 与成本",
        "",
        f"- 输入 token：{r['input_tokens']}",
        f"- 输出 token：{r['output_tokens']}",
        f"- 总 token：{r['total_tokens']}",
        f"- 输入成本：{_money(r['input_cost'], r['currency'])}",
        f"- 输出成本：{_money(r['output_cost'], r['currency'])}",
        f"- 总成本：{_money(r['total_cost'], r['currency'])}",
        f"- 单条平均成本：{_money(r['avg_cost_per_case'], r['currency'])}",
        f"- 价格配置来源：{r['pricing_source']}",
        "",
        "## 说明",
        "",
    ]
    lines += [f"- {n}" for n in r["notes"]]
    if r["mode"] == "candidate" and not measured:
        lines.append(f"- {UNMEASURED_NOTE}")
        lines.append("- 不得将本报告中的任何数值表述为候选模型的真实准确率、真实成本或真实延迟。")
    lines.append(f"- 成本说明：价格为 None 表示**未配置**（表中显示 N/A），"
                 f"不代表 0 成本；离线规则模式不调用模型，token 记为 0。")
    lines += ["", "## 逐条明细", ""]
    lines.append("| case | provider | intent(预测/期望) | 命中 | degraded | error | "
                 "in_tok | out_tok | cost | 耗时ms |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in r["rows"]:
        exp = row["expected_intent"] or "-"
        pred_i = row["prediction"].get("intent", "-")
        lines.append(
            f"| {row['case_id']} | {row['provider']} | {pred_i}/{exp} | "
            f"{'✅' if row['intent_ok'] else '❌'} | {row['degraded']} | "
            f"{row['error'] or '-'} | {row['input_tokens']} | {row['output_tokens']} | "
            f"{_money(row['cost_usd'], row['currency'])} | {row['duration_ms']} |"
        )
    lines.append("")
    lines.append("> 诚实边界：全部为固定随机种子合成数据；报告不含 API Key、完整请求体或客户 PII。"
                 "真实模型必须实际运行后回填，未运行一律标注未实测。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 5A 影子评测")
    parser.add_argument("--mode", choices=["offline", "candidate"], required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--outdir", type=Path, default=None)
    args = parser.parse_args()
    run(mode=args.mode, limit=args.limit, outdir=args.outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
