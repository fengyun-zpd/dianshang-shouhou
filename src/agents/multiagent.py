"""V1.1 四角色只读多 Agent 流程（阶段 3，ADR-002 实验演进）。

把现有三只读子 Agent（order/history/policy，src/agents/subagents.py）实验演进为
四角色只读编排：**Triage → Evidence → Resolution → RiskReview**。四个角色都有
独立 io Schema（Pydantic）、角色说明（role）、工具白名单与 trace_id；全部只读。

诚实边界（与本项目其它模型标注一致）：**四个角色当前均为确定性规则实现，不是真实
LLM**——LLM 未接入（无安全 Key / 不允许 Base URL，见 src/models）；角色契约
（io Schema / role / 白名单）是未来真实 LLM 子 Agent 的挂载点，与 src/models 的
离线基线同属"规则化、可替换、可评测"边界。

确定性约束（宪法第三/四/六条，全部沿用既有语义，不新增事实源）：
- 子角色只有只读工具白名单；写命令只在顶层 Supervisor（gateway → 领域服务 + 人工审批）；
- 金额唯一权威来源 = `gateway.compute_refund_plan`（确定性领域服务）；ResolutionAgent
  只产出"动作草稿意图与说明"，**不产出金额数值**（schema 无金额字段，文本另经内容守卫）；
- RiskReviewer 审查引用有效性 / 租户一致 / 金额来源 / 动作一致性 / 订单状态；
  阻断风险 → 编排转人工（escalate），不猜测继续；
- Evidence 阶段证据不足 / 角色失败 / 超时 → escalate（域错误码原样透传，不改写成成功）；
- checkpoint 仍只存流程状态（复用既有 LangGraph 图，本模块只提供 gather 证据编排节点）。

SupervisorRunner 默认仍使用既有三 agent 编排（`orchestration="three-agent"`，A/B 对照
结论与 compare_agents 不受影响）；四角色是**新增可选编排**
（`SupervisorRunner(..., orchestration="four-role")`），产出与单 Agent
gather_evidence + plan 等价的 AgentState 语义（evidence_refs / order_summary /
risk_level），便于用同一黄金集/AgentState 做 A/B 对照。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Type

from pydantic import BaseModel, Field

from src.domain.after_sales import AfterSalesError
from src.domain.after_sales.models import RefundPlan
from src.domain.models import Role
from src.platform.tooling import TenantContext, ToolRegistry, ToolResult

from .intent import extract_intent
from .ports import AfterSalesGateway
from .state import AgentState
from .toolkit import build_toolkit
from src.rag import PolicyStore

# 意图标签白名单（与 parse 路由一致；unknown/不支持自动草稿 → 不进本编排）
_KNOWN_INTENTS = frozenset({"refund", "return", "exchange", "replace", "upgrade", "unknown"})


# ---------- 角色输出内容守卫（本地轻量实现） ----------
#
# 与 src/models/base.py 的 assert_output_safe 同语义的本地轻量检查：四角色产物为规则
# 模板/结构化文本（不接模型包，避免 Agent 主链路产生对 src/models 的依赖）；金额、
# 审批、状态迁移、执行指令任何一项命中即拒绝，角色/编排不得把此类内容带向写路径。

_MONEY_RE = re.compile(r"[¥￥]|\d+(?:\.\d+)?\s*(?:元|块|人民币|rmb|usd|美元)", re.IGNORECASE)
_ACTION_WORDS = (
    "批准", "审批通过", "同意退款", "执行退款", "退款已执行", "立即退款",
    "已批准", "已拒绝", "关闭工单", "修改地址", "状态已变更", "状态迁移",
)
_EN_ACTION_RE = re.compile(
    r"\b(approve|reject|execute|close\s+ticket|refund\s+now|change\s+address)\b",
    re.IGNORECASE,
)

# trace 摘要脱敏（宪法第五条：日志不得泄露完整 PII；手机/邮箱在摘要写入前掩码）
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class RoleOutputPolicyError(ValueError):
    """角色输出违反内容策略（金额/审批/状态/执行指令），禁止带向写路径。"""


def assert_agent_copy_safe(text: str) -> None:
    """角色可读文本守门：命中金额/审批/状态/执行指令 → RoleOutputPolicyError。"""
    if not text:
        return
    for word in _ACTION_WORDS:
        if word in text:
            raise RoleOutputPolicyError(f"角色输出含禁止动作指令：{word!r}")
    if _MONEY_RE.search(text):
        raise RoleOutputPolicyError("角色输出含金额内容（金额必须来自确定性领域计划）")
    if _EN_ACTION_RE.search(text):
        raise RoleOutputPolicyError("角色输出含禁止的英文动作指令（approve/reject/execute/refund now…）")


def assert_agent_payload_safe(payload: BaseModel) -> None:
    """递归检查结构化输出中的字符串字段（防嵌套 dict/list 藏匿动作或金额）。"""
    def walk(value) -> None:  # noqa: ANN001
        if isinstance(value, str):
            assert_agent_copy_safe(value)
        elif isinstance(value, BaseModel):
            walk(value.model_dump())
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    walk(payload)


# ---------- 四角色 io Schema（独立 Pydantic，结构互不混用） ----------

class TriageInput(BaseModel):
    """TriageAgent 输入：用户请求 + 订单提示（意图规则与 parse 同源）。"""
    request: str
    order_id_hint: Optional[str] = None


class TriageOutput(BaseModel):
    """TriageAgent 输出：意图标签 / 订单提示 / 诉求标签 / 缺参初判。"""
    intent: str
    order_id: Optional[str] = None
    reason_tags: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    notes: str = ""


class EvidenceInput(BaseModel):
    """EvidenceAgent 输入：订单号 + 用户请求 + 诉求标签（检索 query 用语）。"""
    order_id: str
    user_request: str = ""
    reason_tags: list[str] = Field(default_factory=list)
    top_k: int = 3


class EvidenceOutput(BaseModel):
    """EvidenceAgent 输出：订单/历史/政策证据引用（全部只读；物流显式不可用）。"""
    order_id: str
    tenant_id: str
    customer_id: str
    order_status: str
    paid_amount: str
    evidence_refs: list[str] = Field(default_factory=list)
    policy_citations: list[str] = Field(default_factory=list)
    policy_notes: list[str] = Field(default_factory=list)
    history_ticket_ids: list[str] = Field(default_factory=list)
    logistics: dict = Field(default_factory=dict)
    ok: bool = True
    error_code: Optional[str] = None
    error_detail: str = ""


class ResolutionInput(BaseModel):
    """ResolutionAgent 输入：领域退款计划的关键事实（**金额不在此数据面**）。"""
    order_id: str
    draft_intent: str
    reason_tags: list[str] = Field(default_factory=list)
    plan_policy_id: str
    plan_refund_ratio: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class ResolutionOutput(BaseModel):
    """ResolutionAgent 输出：动作草稿意图与说明。

    结构化约束：**无金额字段**；amount_source 恒为 "domain_refund_plan"（金额唯一来源
    声明，非金额数值）；explanation/boundaries 为规则模板，内容守卫兜底拒绝任何
    金额/审批/状态/执行词。
    """
    action_kind: str                      # "refund"（V1 仅退款可自动草稿）
    draft_intent: str
    policy_id: str
    explanation: str = ""
    amount_source: str = "domain_refund_plan"
    boundaries: list[str] = Field(default_factory=list)


class RiskFinding(BaseModel):
    code: str
    detail: str


class RiskReviewInput(BaseModel):
    """RiskReviewer 输入：证据 / 领域计划 / 决议声明（金额来源对照）。"""
    tenant_id: str
    order_id: str
    order_status: str
    customer_id: str = ""
    evidence_tenant_id: Optional[str] = None   # EvidenceOutput.tenant_id（跨租户对照）
    amount_source: str
    plan_policy_id: str
    resolution_policy_id: str
    resolution_action_kind: str
    resolution_draft_intent: str
    evidence_refs: list[str] = Field(default_factory=list)
    policy_citations: list[str] = Field(default_factory=list)


class RiskReviewOutput(BaseModel):
    """RiskReviewer 输出：risk_ok / risk_level / 风险项（阻断才转人工）。"""
    risk_ok: bool
    risk_level: str                       # "high"（退款草稿高危，始终需人工审批）
    findings: list[RiskFinding] = Field(default_factory=list)


# ---------- 角色声明 ----------

@dataclass(frozen=True)
class RoleAgentSpec:
    """四角色统一契约：名称 / 角色说明 / 只读工具白名单 / 独立 io Schema。"""
    name: str
    role: str
    allowed_tools: tuple[str, ...]
    input_schema: Type[BaseModel]
    output_schema: Type[BaseModel]
    description: str = ""


TRIAGE_AGENT_SPEC = RoleAgentSpec(
    name="triage-agent",
    role="售后意图与缺参初判（只读；不调用任何工具）",
    allowed_tools=(),
    input_schema=TriageInput,
    output_schema=TriageOutput,
    description="规则化意图分类：输出意图标签 / order_id 提示 / reason_tags / missing_fields",
)

EVIDENCE_AGENT_SPEC = RoleAgentSpec(
    name="evidence-agent",
    role="只读证据检索：订单 / 历史工单 / 政策引用（物流显式不可用，不虚构）",
    allowed_tools=("get_order", "list_customer_tickets", "retrieve_policy"),
    input_schema=EvidenceInput,
    output_schema=EvidenceOutput,
    description="并行只读证据编排：订单核验 → 历史工单 → 政策引用，输出带 citation 的证据引用",
)

RESOLUTION_AGENT_SPEC = RoleAgentSpec(
    name="resolution-agent",
    role="动作草稿意图与说明（金额必须来自确定性领域计划，角色不产出金额）",
    allowed_tools=(),
    input_schema=ResolutionInput,
    output_schema=ResolutionOutput,
    description="基于领域退款计划产出草稿说明（退款/说明），金额由领域计划填充",
)

RISK_REVIEWER_SPEC = RoleAgentSpec(
    name="risk-reviewer",
    role="风险审查：引用有效性 / 租户一致 / 金额来源 / 动作一致性 / 订单状态",
    allowed_tools=(),
    input_schema=RiskReviewInput,
    output_schema=RiskReviewOutput,
    description="阻断风险（跨租户 / 引用无效 / 金额非领域来源等）→ 转人工，不猜测继续",
)

FOUR_ROLE_SPECS: tuple[RoleAgentSpec, ...] = (
    TRIAGE_AGENT_SPEC, EVIDENCE_AGENT_SPEC, RESOLUTION_AGENT_SPEC, RISK_REVIEWER_SPEC,
)


class AgentStageError(RuntimeError):
    """角色执行失败（证据不足 / 域错误 / 越权等）→ 编排转人工（escalate）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class RoleAgent:
    """四角色统一基类：构造即校验白名单只读；工具调用越界即 PERMISSION_DENIED。

    - allowed_tools 中的工具必须在 registry 存在且 read_only=True（否则构造抛 ValueError）；
    - 不提供 registry 时允许白名单必须为空（Triage / Resolution / RiskReviewer 无工具）。
    """

    def __init__(self, spec: RoleAgentSpec, registry: Optional[ToolRegistry] = None):
        if not spec.name or not spec.role:
            raise ValueError("角色声明缺少 name/role")
        self.spec = spec
        self._registry = registry
        if spec.allowed_tools:
            if registry is None:
                raise ValueError(f"角色 {spec.name} 白名单非空但未提供 ToolRegistry")
            available = {t["name"]: t for t in registry.list_tools()}
            for tool in spec.allowed_tools:
                if tool not in available:
                    raise ValueError(f"角色 {spec.name}：工具 {tool} 未注册")
                if not available[tool]["read_only"]:
                    raise ValueError(f"角色 {spec.name}：工具 {tool} 不是只读，禁止白名单化")

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def role(self) -> str:
        return self.spec.role

    @property
    def tool_names(self) -> list[str]:
        return list(self.spec.allowed_tools)

    def call_tool(self, ctx: TenantContext, tool: str, args: dict) -> ToolResult:
        """只允许白名单工具；越界（含未注册/写工具）→ PERMISSION_DENIED。"""
        if tool not in self.spec.allowed_tools:
            return ToolResult.error("PERMISSION_DENIED",
                                    f"角色 {self.spec.name} 无权调用工具 {tool}")
        return self._registry.invoke(ctx, tool, args)


class TriageAgent(RoleAgent):
    """TriageAgent：意图/缺参初判（规则，与 parse 同源；无工具、无写路径）。"""

    def __init__(self):
        super().__init__(TRIAGE_AGENT_SPEC, registry=None)

    def run(self, input_: TriageInput) -> TriageOutput:
        res = extract_intent(input_.request, input_.order_id_hint)
        intent = res["intent"] if res["intent"] in _KNOWN_INTENTS else "unknown"
        out = TriageOutput(
            intent=intent,
            order_id=res["order_id"],
            reason_tags=res["reason_tags"],
            missing_fields=res["missing_fields"],
            notes="意图/缺参初判为确定性规则（离线）；最终澄清与路由以流程 parse 为准",
        )
        assert_agent_payload_safe(out)
        return out


class EvidenceAgent(RoleAgent):
    """EvidenceAgent：只读证据检索（订单 → 历史工单 → 政策引用；物流显式不可用）。

    失败语义：
    - 订单核验失败（域错误：不存在/跨租户）→ AgentStageError，域错误码原样透传；
    - 政策检索：无证据（NO_EVIDENCE）不阻断（领域资格为准，记录 policy_notes）；
      注入命中（INJECTION_DETECTED）→ AgentStageError（不把注入当证据继续）。
    """

    def __init__(self, registry: ToolRegistry):
        super().__init__(EVIDENCE_AGENT_SPEC, registry=registry)

    def run(self, ctx: TenantContext, input_: EvidenceInput) -> EvidenceOutput:
        order_result = self.call_tool(ctx, "get_order", {"order_id": input_.order_id})
        if not order_result.ok:
            raise AgentStageError(
                order_result.error_code or "EVIDENCE_FAILED",
                f"订单核验失败（{order_result.detail}），已转人工，不继续生成草稿",
            )
        order = order_result.data

        history_result = self.call_tool(ctx, "list_customer_tickets",
                                        {"customer_id": order["customer_id"]})
        ticket_ids = history_result.data["ticket_ids"] if history_result.ok else []

        policy_query = (input_.user_request.strip() or " ".join(input_.reason_tags)) + " 售后政策"
        policy_result = self.call_tool(ctx, "retrieve_policy",
                                       {"query": policy_query, "top_k": input_.top_k})
        policy_notes: list[str] = []
        citations: list[str] = []
        if policy_result.ok:
            citations = [item["citation"] for item in policy_result.data["results"]]
        else:
            policy_notes.append(policy_result.error_code or "POLICY_UNAVAILABLE")
            if policy_result.error_code == "INJECTION_DETECTED":
                raise AgentStageError(
                    "POLICY_INJECTION_DETECTED",
                    "政策检索请求命中提示注入模式，检索已被安全拒绝；"
                    "未使用任何检索内容作为证据，已转人工处理",
                )

        refs = [f"order:{order['order_id']}", f"customer:{order['customer_id']}"]
        refs += [f"history:{t}" for t in ticket_ids]
        refs += [f"policy:{c}" for c in citations]
        refs.append("logistics:unavailable")  # 物流未接入，显式记录而非虚构

        return EvidenceOutput(
            order_id=order["order_id"],
            tenant_id=order["tenant_id"],
            customer_id=order["customer_id"],
            order_status=order["status"],
            paid_amount=order["paid_amount"],
            evidence_refs=refs,
            policy_citations=citations,
            policy_notes=policy_notes,
            history_ticket_ids=ticket_ids,
            logistics={"available": False, "tenant_id": ctx.tenant_id,
                       "order_id": order["order_id"], "reason": "物流查询未接入（显式不可用）"},
            ok=True,
        )


class ResolutionAgent(RoleAgent):
    """ResolutionAgent：只生成动作草稿意图与说明。

    - 金额唯一权威来源 = 领域 RefundPlan（由编排器经 gateway.compute_refund_plan 获得，
      不经过角色数据面；本角色输入不含金额、输出无金额字段）；
    - explanation/boundaries 为规则模板；结构输出过 assert_agent_payload_safe 内容守卫。
    """

    def __init__(self):
        super().__init__(RESOLUTION_AGENT_SPEC, registry=None)

    def run(self, input_: ResolutionInput) -> ResolutionOutput:
        action_kind = "refund"  # V1 仅退款可自动草稿；其它意图在 parse/风险阶段拦截
        explanation = (
            f"建议按适用政策 {input_.plan_policy_id} 生成退款动作草稿；"
            "退款金额由确定性领域计划按政策比例计算并填充，本角色不产出金额；"
            "草稿需经授权人员审批后方可执行。"
        )
        out = ResolutionOutput(
            action_kind=action_kind,
            draft_intent=input_.draft_intent,
            policy_id=input_.plan_policy_id,
            explanation=explanation,
            amount_source="domain_refund_plan",
            boundaries=[
                "金额由确定性领域计划填充，角色不产出金额",
                "审批决定由授权人员提交到领域服务，角色不做出审批",
                "订单状态与执行结果由领域服务裁决，角色不改变状态",
            ],
        )
        assert_agent_payload_safe(out)  # 内容守卫兜底：任何金额/审批/状态/执行词 → 拒绝
        return out


class RiskReviewer(RoleAgent):
    """RiskReviewer：审查证据引用 / 租户一致 / 金额来源 / 动作一致性 / 订单状态。

    阻断风险（risk_ok=False，编排转人工）：
    - RISK_CROSS_TENANT：证据 tenant 与流程租户不一致（跨租户引用/越权）；
    - RISK_EVIDENCE_INCOMPLETE：证据引用缺少订单引用（order:），证据链不完整；
    - RISK_CITATION_INVALID：policy citation 存在但无法在 policy_store 校验
      （含跨租户/停用/畸形引用——store.validate_citation 按租户校验）；
    - RISK_AMOUNT_NOT_FROM_DOMAIN：金额来源不是 "domain_refund_plan"（疑似模型/伪造）；
    - RISK_PLAN_MISMATCH：决议 policy/动作与领域计划不一致；
    - RISK_ORDER_NOT_ELIGIBLE：订单状态不可售后；
    - RISK_UNSUPPORTED_ACTION：非退款自动草稿（防御，parse 已拦截）。
    提示级（advisory，不阻断；领域政策仍为准）：
    - ADVISORY_NO_POLICY_CITATION / ADVISORY_LOGISTICS_UNAVAILABLE。
    """

    def __init__(self, policy_store: Optional[PolicyStore] = None):
        super().__init__(RISK_REVIEWER_SPEC, registry=None)
        self._policy_store = policy_store

    def review(self, input_: RiskReviewInput) -> RiskReviewOutput:
        findings: list[RiskFinding] = []
        blocking_codes = {"RISK_CROSS_TENANT", "RISK_EVIDENCE_INCOMPLETE",
                          "RISK_CITATION_INVALID", "RISK_AMOUNT_NOT_FROM_DOMAIN",
                          "RISK_PLAN_MISMATCH", "RISK_ORDER_NOT_ELIGIBLE",
                          "RISK_UNSUPPORTED_ACTION"}

        if input_.evidence_tenant_id and input_.evidence_tenant_id != input_.tenant_id:
            findings.append(RiskFinding(
                code="RISK_CROSS_TENANT",
                detail=f"证据租户 {input_.evidence_tenant_id} 与流程租户 "
                       f"{input_.tenant_id} 不一致（跨租户引用/越权）"))
        if input_.order_status == "closed":
            findings.append(RiskFinding(code="RISK_ORDER_NOT_ELIGIBLE",
                                        detail=f"订单 {input_.order_id} 已关闭，不可售后"))
        if input_.amount_source != "domain_refund_plan":
            findings.append(RiskFinding(
                code="RISK_AMOUNT_NOT_FROM_DOMAIN",
                detail=f"金额来源 {input_.amount_source!r} 不是确定性领域计划（domain_refund_plan）"))
        if input_.resolution_policy_id != input_.plan_policy_id:
            findings.append(RiskFinding(
                code="RISK_PLAN_MISMATCH",
                detail=f"决议政策 {input_.resolution_policy_id} 与领域计划政策 "
                       f"{input_.plan_policy_id} 不一致"))
        if input_.resolution_action_kind != "refund" or input_.resolution_draft_intent != "refund":
            findings.append(RiskFinding(
                code="RISK_UNSUPPORTED_ACTION",
                detail=f"不支持自动草稿的动作：action={input_.resolution_action_kind!r} "
                       f"intent={input_.resolution_draft_intent!r}"))
        # 引用结构：order 引用必须存在（证据链起点），否则视为不完整
        if not any(ref.startswith("order:") for ref in input_.evidence_refs):
            findings.append(RiskFinding(code="RISK_EVIDENCE_INCOMPLETE",
                                        detail="证据引用缺少订单引用（order:），证据链不完整"))
        # policy citation 校验（store 可用时；跨租户/无效/停用/畸形 → 阻断）
        if self._policy_store is not None:
            for citation in input_.policy_citations:
                if self._policy_store.validate_citation(input_.tenant_id, citation) is None:
                    findings.append(RiskFinding(
                        code="RISK_CITATION_INVALID",
                        detail=f"政策引用不可校验：{citation}（tenant={input_.tenant_id}）"))
            if not input_.policy_citations:
                findings.append(RiskFinding(
                    code="ADVISORY_NO_POLICY_CITATION",
                    detail="无政策引用（RAG 无证据或未检索到）；领域政策判定仍为准，未猜测"))
        else:
            if input_.policy_citations:
                findings.append(RiskFinding(
                    code="ADVISORY_NO_POLICY_CITATION",
                    detail="政策引用存在但无 policy_store 可校验（提示级，不阻断）"))

        has_logistics_note = any(ref == "logistics:unavailable" for ref in input_.evidence_refs)
        if has_logistics_note:
            findings.append(RiskFinding(code="ADVISORY_LOGISTICS_UNAVAILABLE",
                                        detail="物流查询显式不可用（不虚构物流状态）"))

        risk_ok = not any(f.code in blocking_codes for f in findings)
        return RiskReviewOutput(
            risk_ok=risk_ok,
            risk_level="high",  # 退款动作草稿高危：通过审查也始终进入人工审批展示
            findings=findings,
        )


# ---------- 四角色编排（Supervisor 可选运行时） ----------

class FourRoleOrchestrator:
    """四角色编排器：Triage → Evidence → Resolution（领域金额）→ RiskReview。

    输出与单 Agent gather_evidence + plan 等价的 AgentState 语义：
    evidence_refs / order_summary / risk_level（形状与单 Agent 一致 + supervisor 元数据）；
    证据不足 / 角色失败 / 阻断风险 → escalate（error_code + outcome=escalated +
    next_action=escalate），不猜测继续。
    """

    def __init__(self, gateway: AfterSalesGateway,
                 policy_store: Optional[PolicyStore] = None):
        self.gateway = gateway
        self.policy_store = policy_store
        self._registry = build_toolkit(gateway,
                                       policy_store if policy_store is not None else PolicyStore())
        self.triage = TriageAgent()
        self.evidence = EvidenceAgent(self._registry)
        self.resolution = ResolutionAgent()
        self.risk_reviewer = RiskReviewer(policy_store=policy_store)

    def run(self, state: AgentState) -> dict:
        """按序执行四角色并收集结构化 trace（真实执行、真实耗时、无 PII 摘要）。

        每条 trace 由编排器记录（trace_id=ma-{thread}:{agent_name}、input/output
        摘要截断、started/finished 为相对编排起点的单调秒、status、reject_reason、
        citations、tool_calls）；成功路径挂在 order_summary.supervisor.traces，
        失败（escalate）路径同样以 order_summary.supervisor.traces 随返回携带——
        不改变既有输出 schema 与既有测试语义（仅追加 supervisor 内字段）。
        """
        tenant = state.get("tenant_id")
        order_id = state.get("order_id")
        thread = state.get("thread_id", "x")
        trace_root = f"ma-{thread}"
        traces: list[dict] = []
        t0 = time.perf_counter_ns()

        def _trace(spec: RoleAgentSpec, t_start: int, *, status: str,
                   reject_reason: str = "", input_summary: str = "",
                   output_summary: str = "", citations=None, tool_calls=None) -> None:
            now = time.perf_counter_ns()
            traces.append({
                "trace_id": f"{trace_root}:{spec.name}",
                "thread_id": thread,
                "agent_name": spec.name,
                "role": spec.role,
                "input_summary": self._clip(self._sanitize(input_summary), 160),
                "output_summary": self._clip(self._sanitize(output_summary), 220),
                "started_at": f"{(t_start - t0) / 1e9:.3f}",
                "finished_at": f"{(now - t0) / 1e9:.3f}",
                "duration_ms": round((now - t_start) / 1e6, 3),
                "status": status,
                "reject_reason": reject_reason or "",
                "citations": list(citations or []),
                "tool_calls": list(tool_calls or []),
            })

        def _escalate(code: str, message: str,
                      risk_blocking: Optional[list[str]] = None) -> dict:
            """escalate 返回（error_code/outcome/next_action/reply + traces 随 order_summary）。"""
            supervisor: dict = {
                "orchestration": "four-role",
                "agents": [
                    {"name": s.name, "role": s.role, "trace_id": f"{trace_root}:{s.name}"}
                    for s in FOUR_ROLE_SPECS
                ],
                "traces": list(traces),
                "escalated": {"error_code": code,
                              "detail": self._clip(self._sanitize(message), 240)},
            }
            if risk_blocking:
                supervisor["risk"] = {
                    "risk_ok": False,
                    "risk_level": "high",
                    "findings": [{"code": c, "detail": "风险审查阻断（编排转人工）"}
                                 for c in risk_blocking],
                }
            return {"error_code": code, "outcome": "escalated",
                    "next_action": "escalate", "reply": message,
                    "order_summary": {"supervisor": supervisor}}

        if not tenant or not order_id:
            for spec in FOUR_ROLE_SPECS:
                _trace(spec, t0, status="skipped", reject_reason="MISSING_ORDER_ID",
                       output_summary="未执行（缺少订单号，编排不猜测）")
            return _escalate("MISSING_ORDER_ID",
                             "缺少订单号，无法核验（应已澄清），已转人工，不猜测继续")
        if state.get("missing_fields"):
            for spec in FOUR_ROLE_SPECS:
                _trace(spec, t0, status="skipped",
                       reject_reason="MISSING_REQUIRED_FIELD",
                       output_summary="未执行（存在未澄清缺参，应已在 clarify 阶段补全）")
            return _escalate("MISSING_REQUIRED_FIELD",
                             "存在未澄清缺参（应已在 clarify 阶段补全），已转人工")

        reason_tags = tuple(state.get("reason_tags") or [])
        user_request = state.get("user_request") or ""
        draft_intent = state.get("intent") or "unknown"
        ctx = TenantContext(tenant_id=tenant, actor=Role.AGENT,
                            trace_id=f"{trace_root}:evidence-agent")

        # 1) Triage（与 parse 同源规则；记录供对照，流程参数以 state 为准）
        t_start = time.perf_counter_ns()
        triage_output = self.triage.run(TriageInput(request=user_request, order_id_hint=order_id))
        triage_record = {
            "intent": triage_output.intent,
            "order_id": triage_output.order_id,
            "reason_tags": list(triage_output.reason_tags),
            "missing_fields": list(triage_output.missing_fields),
        }
        _trace(TRIAGE_AGENT_SPEC, t_start, status="ok",
               input_summary=(f"request={self._clip(user_request, 100) or '(空)'}; "
                              f"order_hint={order_id or '-'}"),
               output_summary=(f"intent={triage_output.intent}; "
                               f"order={triage_output.order_id or '-'}; "
                               f"reason_tags={list(triage_output.reason_tags)}; "
                               f"missing_fields={list(triage_output.missing_fields)}"))

        # 2) Evidence（只读白名单；订单核验失败/注入 → AgentStageError → escalate）
        t_start = time.perf_counter_ns()
        try:
            evidence = self.evidence.run(ctx, EvidenceInput(
                order_id=order_id, user_request=user_request,
                reason_tags=list(reason_tags), top_k=3,
            ))
        except AgentStageError as e:
            _trace(EVIDENCE_AGENT_SPEC, t_start, status="error",
                   reject_reason=e.code,
                   input_summary=f"order={order_id}; tags={list(reason_tags)}; top_k=3",
                   output_summary=(f"证据检索被安全拒绝，未产出引用"
                                   f"（{self._clip(e.message, 160)}）"),
                   tool_calls=["get_order"])  # 失败发生在 get_order 阶段（域错误/注入在检索前）
            for spec in (RESOLUTION_AGENT_SPEC, RISK_REVIEWER_SPEC):
                _trace(spec, t_start, status="skipped", reject_reason=e.code,
                       output_summary="未执行（上游证据阶段失败，编排不继续）")
            return _escalate(e.code, e.message)
        _trace(EVIDENCE_AGENT_SPEC, t_start, status="ok",
               input_summary=f"order={order_id}; tags={list(reason_tags)}; top_k=3",
               output_summary=(f"order={evidence.order_id}; customer={evidence.customer_id}; "
                               f"status={evidence.order_status}; "
                               f"history_tickets={len(evidence.history_ticket_ids)}; "
                               f"evidence_refs={len(evidence.evidence_refs)}; "
                               f"policy_citations={len(evidence.policy_citations)}; "
                               f"policy_notes={evidence.policy_notes or '[]'}"),
               citations=evidence.policy_citations,
               tool_calls=["get_order", "list_customer_tickets", "retrieve_policy"])

        # 3) 确定性退款计划（金额唯一权威来源；域错误原样透传，不改写成成功）
        t_start = time.perf_counter_ns()
        try:
            plan: RefundPlan = self.gateway.compute_refund_plan(tenant, order_id, reason_tags)
        except AfterSalesError as e:
            _trace(RESOLUTION_AGENT_SPEC, t_start, status="error",
                   reject_reason=e.code.value,
                   input_summary=f"order={order_id}; draft_intent={draft_intent}",
                   output_summary=(f"领域退款计划失败（{self._clip(e.message, 160)}），"
                                   f"草稿未生成（域错误码原样透传）"))
            _trace(RISK_REVIEWER_SPEC, t_start, status="skipped",
                   reject_reason=e.code.value,
                   output_summary="未执行（决议阶段失败，编排不继续）")
            return _escalate(e.code.value,
                             f"无法生成退款方案（{e.message}），已转人工，不继续生成草稿")

        # 4) Resolution（基于领域计划；金额不进角色数据面，输出经内容守卫）
        t_start = time.perf_counter_ns()
        try:
            resolution = self.resolution.run(ResolutionInput(
                order_id=order_id,
                draft_intent=draft_intent,
                reason_tags=list(reason_tags),
                plan_policy_id=plan.policy_id,
                plan_refund_ratio=str(plan.refund_ratio),
                evidence_refs=evidence.evidence_refs,
            ))
        except RoleOutputPolicyError as e:
            _trace(RESOLUTION_AGENT_SPEC, t_start, status="error",
                   reject_reason="ROLE_OUTPUT_POLICY_VIOLATION",
                   input_summary=f"order={order_id}; plan_policy={plan.policy_id}",
                   output_summary=(f"决议输出违反内容策略（{self._clip(str(e), 160)}），"
                                   f"草稿未生成"))
            _trace(RISK_REVIEWER_SPEC, t_start, status="skipped",
                   reject_reason="ROLE_OUTPUT_POLICY_VIOLATION",
                   output_summary="未执行（决议阶段失败，编排不继续）")
            return _escalate("ROLE_OUTPUT_POLICY_VIOLATION",
                             f"决议角色输出违反内容策略（{e}），已转人工")
        _trace(RESOLUTION_AGENT_SPEC, t_start, status="ok",
               input_summary=(f"order={order_id}; draft_intent={draft_intent}; "
                              f"plan_policy={plan.policy_id}; "
                              f"evidence_refs={len(evidence.evidence_refs)}"),
               output_summary=(f"action_kind={resolution.action_kind}; "
                               f"policy={resolution.policy_id}; "
                               f"amount_source={resolution.amount_source}；"
                               f"角色不产出金额（金额唯一来源=领域退款计划）"))

        # 5) RiskReview（阻断风险 → escalate；高危动作草稿仍需人工审批展示）
        t_start = time.perf_counter_ns()
        review = self.risk_reviewer.review(RiskReviewInput(
            tenant_id=tenant,
            order_id=order_id,
            order_status=evidence.order_status,
            customer_id=evidence.customer_id,
            evidence_tenant_id=evidence.tenant_id,
            amount_source=resolution.amount_source,
            plan_policy_id=plan.policy_id,
            resolution_policy_id=resolution.policy_id,
            resolution_action_kind=resolution.action_kind,
            resolution_draft_intent=resolution.draft_intent,
            evidence_refs=evidence.evidence_refs,
            policy_citations=evidence.policy_citations,
        ))
        blocking_codes = [f.code for f in review.findings
                          if f.code.startswith("RISK_")]
        _trace(RISK_REVIEWER_SPEC, t_start,
               status="escalated" if not review.risk_ok else "ok",
               reject_reason="、".join(blocking_codes) if not review.risk_ok else "",
               input_summary=(f"tenant={tenant}; order={order_id}; "
                              f"status={evidence.order_status}; "
                              f"evidence_tenant={evidence.tenant_id}; "
                              f"amount_source={resolution.amount_source}; "
                              f"plan_policy={plan.policy_id}; "
                              f"resolution_policy={resolution.policy_id}"),
               output_summary=(f"risk_ok={review.risk_ok}; "
                               f"level={review.risk_level}; "
                               f"findings={[f.code for f in review.findings]}"),
               citations=evidence.policy_citations)
        if not review.risk_ok:
            return _escalate(
                "RISK_REVIEW_FAILED",
                f"风险审查未通过（{'、'.join(blocking_codes)}），已转人工，不继续生成草稿",
                risk_blocking=blocking_codes,
            )

        return {
            "evidence_refs": evidence.evidence_refs,
            "order_summary": self._build_summary(state, evidence, resolution, review,
                                                 triage_record, trace_root, traces),
            "risk_level": review.risk_level,
        }

    @staticmethod
    def _sanitize(text) -> str:
        """trace 摘要脱敏：手机号/邮箱掩码（不落完整 PII；合成 ID/订单号保留为领域摘要）。"""
        s = str(text or "")
        s = _PHONE_RE.sub("<phone>", s)
        s = _EMAIL_RE.sub("<email>", s)
        return s

    @staticmethod
    def _clip(text, limit: int = 160) -> str:
        """trace 摘要截断（不落完整请求/长说明）。"""
        s = str(text or "")
        return s if len(s) <= limit else s[:limit] + "…"

    @staticmethod
    def _build_summary(state: AgentState, evidence: EvidenceOutput,
                       resolution: ResolutionOutput, review: RiskReviewOutput,
                       triage_record: dict, trace_root: str,
                       traces: Optional[list[dict]] = None) -> dict:
        return {
            "order_id": evidence.order_id,
            "customer_id": evidence.customer_id,
            "status": evidence.order_status,
            "paid_amount": evidence.paid_amount,
            "logistics": evidence.logistics,
            "history_count": len(evidence.history_ticket_ids),
            "policy_citations": evidence.policy_citations,
            "policy_notes": evidence.policy_notes,
            "supervisor": {
                "orchestration": "four-role",
                "agents": [
                    {"name": spec.name, "role": spec.role, "trace_id": f"{trace_root}:{spec.name}"}
                    for spec in FOUR_ROLE_SPECS
                ],
                "triage": triage_record,
                "resolution": {
                    "action_kind": resolution.action_kind,
                    "draft_intent": resolution.draft_intent,
                    "policy_id": resolution.policy_id,
                    "amount_source": resolution.amount_source,
                },
                "risk": {
                    "risk_ok": review.risk_ok,
                    "risk_level": review.risk_level,
                    "findings": [{"code": f.code, "detail": f.detail} for f in review.findings],
                },
                "traces": list(traces or []),
            },
        }


def build_four_role_orchestrator(gateway: AfterSalesGateway,
                                 policy_store: Optional[PolicyStore] = None):
    """构造四角色编排节点（LangGraph gather_evidence 替换节点：state → state 增量）。

    policy_store 为 None：政策检索用空 store（无证据不阻断，记录 NO_EVIDENCE），
    RiskReviewer 无 store 可校验 → 政策引用提示级（本路径下无引用产生）。
    """
    orchestrator = FourRoleOrchestrator(gateway, policy_store)

    def four_role_node(state: AgentState) -> dict:
        return orchestrator.run(state)

    return four_role_node
