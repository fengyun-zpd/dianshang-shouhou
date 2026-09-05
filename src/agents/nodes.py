"""单 Agent 工作流节点（LangGraph StateGraph）。

每个节点只做编排与展示：把确定性领域服务作为唯一事实源。
- Agent 不决定金额（金额来自 gateway.compute_refund_plan 权威结果）；
- Agent 不改状态、不伪造审批（审批决定由 APPROVER 提交到领域服务，resume 后重读）；
- 所有写操作经 gateway 以固定角色进入领域服务（权限/政策/金额/版本/幂等/审计）。
"""
from __future__ import annotations

from typing import Callable, Optional
from uuid import uuid4

from langgraph.types import interrupt

from src.domain.after_sales import AfterSalesError
from src.domain.after_sales.models import OperationStatus
from src.rag.store import InjectionDetected, PolicyStore

from .intent import clarify_questions, extract_intent
from .ports import AfterSalesGateway
from .state import (
    APPROVAL_INTERRUPT_TYPE,
    CLARIFY_INTERRUPT_TYPE,
    AgentState,
    new_state,
)

# 不支持自动草稿的动作（V1 仅退款；补发/换货/升级 → 转人工，不伪造草稿）
_UNSUPPORTED_DRAFT_INTENTS = {"return", "exchange", "replace", "upgrade"}

_INTENT_LABEL = {
    "refund": "退款", "return": "退货", "exchange": "换货",
    "replace": "补发", "upgrade": "升级", "unknown": "无法识别",
}


def _reason_text(state: AgentState, limit: int = 120) -> str:
    req = (state.get("user_request") or "").strip()
    tags = "、".join(state.get("reason_tags") or [])
    text = req or f"售后诉求（标签：{tags}）"
    return text[:limit]


def build_nodes(gateway: AfterSalesGateway,
                policy_store: Optional[PolicyStore] = None) -> dict[str, Callable[[AgentState], dict]]:
    """构建全部节点。gateway / policy_store 经闭包注入（不进入 checkpoint）。

    policy_store 为可选政策证据检索（PolicyStore，纯内存单测可独立构造）；
    不提供时 gather_evidence 与既有基线完全一致（不检索、不新增字段）。
    """

    # ---------- 入口：意图识别 / 缺参判断 ----------

    def parse(state: AgentState) -> dict:
        res = extract_intent(state.get("user_request", ""), state.get("order_id"))
        return dict(res)

    def clarify(state: AgentState) -> dict:
        qs = clarify_questions(state.get("missing_fields") or [])
        extra = interrupt({"type": CLARIFY_INTERRUPT_TYPE, "questions": qs,
                           "missing_fields": state.get("missing_fields", [])})
        merged_req = state.get("user_request", "")
        order_id = state.get("order_id")
        if isinstance(extra, dict):
            order_id = extra.get("order_id") or order_id
            if extra.get("description"):
                merged_req = str(extra["description"])
        elif extra is not None:
            merged_req = str(extra)
        # 清空缺参后回到 parse，由 parse 再次抽取（补充信息若仍不足则再次澄清）
        return {"questions": qs, "order_id": order_id,
                "user_request": merged_req, "missing_fields": []}

    # ---------- 只读证据编排 ----------

    def gather_evidence(state: AgentState) -> dict:
        tenant = state.get("tenant_id")
        order_id = state.get("order_id")
        if not tenant or not order_id:
            return {"error_code": "MISSING_ORDER_ID", "outcome": "escalated",
                    "next_action": "escalate", "reply": "缺少订单号，无法核验（应已澄清）"}
        try:
            order = gateway.get_order(tenant, order_id)
        except AfterSalesError as e:
            return {"error_code": e.code.value, "outcome": "escalated",
                    "next_action": "escalate",
                    "reply": f"订单核验未通过（{e.message}），已转人工，不继续生成草稿"}
        history = gateway.history_tickets(tenant, order.customer_id)
        logistics = gateway.logistics_status(tenant, order_id)
        refs = [f"order:{order.order_id}", f"customer:{order.customer_id}"]
        refs += [f"history:{t.ticket_id}" for t in history]
        refs.append("logistics:unavailable")  # 物流未接入，显式记录而非虚构
        summary: dict = {
            "order_id": order.order_id,
            "customer_id": order.customer_id,
            "status": order.status.value,
            "paid_amount": str(order.paid_amount),
            "logistics": logistics,
            "history_count": len(history),
        }
        # 最小政策证据检索（可选注入；与 Supervisor policy-agent 语义同构）：
        # - 只做证据引用与解释，金额/资格仍由 compute_refund_plan 领域服务裁决；
        # - 查询注入 → 安全拒绝并转人工（不把注入内容当作证据继续）；
        # - 无证据/空结果不阻断主流程（领域政策为准），显式记录 NO_EVIDENCE。
        if policy_store is not None:
            user_request = state.get("user_request") or ""
            query = (user_request.strip() or " ".join(state.get("reason_tags") or [])) + " 售后政策"
            try:
                hits = policy_store.search(tenant, query, top_k=3)
            except InjectionDetected:
                # 安全优先：查询命中注入模式，检索被拒绝 → escalate，不产出任何政策证据
                return {
                    "error_code": "POLICY_INJECTION_DETECTED",
                    "outcome": "escalated",
                    "next_action": "escalate",
                    "reply": "政策检索请求命中提示注入模式，检索已被安全拒绝；"
                            "未使用任何检索内容作为证据，已转人工处理",
                }
            if hits:
                citations = [ev.citation() for ev in hits]
                refs += [f"policy:{c}" for c in citations]
                summary["policy_citations"] = citations
                summary["policy_notes"] = []
            else:
                summary["policy_citations"] = []
                summary["policy_notes"] = ["NO_EVIDENCE"]
        return {
            "evidence_refs": refs,
            "order_summary": summary,
        }

    # ---------- 确定性退款计划（金额唯一来源） ----------

    def plan(state: AgentState) -> dict:
        tenant = state.get("tenant_id")
        order_id = state.get("order_id")
        tags = tuple(state.get("reason_tags") or [])
        if not order_id:
            return {"error_code": "MISSING_ORDER_ID", "outcome": "escalated",
                    "next_action": "escalate", "reply": "缺少订单号，无法生成退款计划"}
        try:
            p = gateway.compute_refund_plan(tenant, order_id, tags)
        except AfterSalesError as e:
            return {"error_code": e.code.value, "outcome": "escalated",
                    "next_action": "escalate",
                    "reply": f"无法生成退款计划（{e.message}），已转人工"}
        draft = {
            "op_type": "refund",
            "amount": str(p.amount),
            "refund_ratio": str(p.refund_ratio),
            "policy_id": p.policy_id,
            "order_id": order_id,
            "note": "金额由确定性领域政策规则计算，Agent 不自行决定",
        }
        return {"action_draft": draft, "risk_level": "high", "next_action": "draft_ready"}

    # ---------- 草稿落库（领域服务）→ 进入人工审批 ----------

    def create_ticket_draft(state: AgentState) -> dict:
        tenant = state.get("tenant_id")
        order_id = state.get("order_id")
        thread_id = state.get("thread_id", "default")
        tags = tuple(state.get("reason_tags") or [])
        ticket_id = state.get("ticket_id")
        op_id = state.get("operation_id")

        if not ticket_id:
            order = gateway.get_order(tenant, order_id)
            ticket = gateway.create_ticket(
                tenant, order_id, order.customer_id,
                reason=_reason_text(state), reason_tags=tags,
                idempotency_key=f"wf:{thread_id}:ticket",
            )
            ticket_id = ticket.ticket_id

        if not op_id:
            # 金额以领域权威计算为准（忽略任何对 action_draft.amount 的注入/篡改）
            p = gateway.compute_refund_plan(tenant, order_id, tags)
            op = gateway.create_refund(
                tenant, ticket_id, p.amount,
                reason_detail=f"Agent 草稿：破损/售后退款（政策 {p.policy_id}）",
                idempotency_key=f"wf:{thread_id}:refund:{ticket_id}",
            )
            op_id = op.operation_id

        op = gateway.submit(tenant, op_id)  # DRAFT → PENDING_APPROVAL（草稿已落库，网关幂等）
        approval_id = state.get("approval_id") or f"APR-{uuid4().hex[:10]}"
        if op.status != OperationStatus.PENDING_APPROVAL:
            # 重复请求且原操作已终态/未知：不再请求审批，直接收尾（幂等，不重复副作用）
            outcome_by_status = {
                OperationStatus.EXECUTED: "already_executed",
                OperationStatus.REJECTED: "already_rejected",
                OperationStatus.FAILED: "already_failed",
                OperationStatus.UNKNOWN: "operation_unknown",
            }
            outcome = outcome_by_status.get(op.status, "already_processed")
            return {
                "ticket_id": ticket_id,
                "operation_id": op_id,
                "approval_id": approval_id,
                "next_action": "finished",
                "outcome": outcome,
                "reply": f"该请求已处理（操作 {op_id} 状态 {op.status.value}），不重复执行",
                "audit_event_ids": state.get("audit_event_ids", []) + gateway.collect_audit_events(tenant),
            }
        return {
            "ticket_id": ticket_id,
            "operation_id": op_id,
            "approval_id": approval_id,
            "risk_level": state.get("risk_level") or "high",
            "next_action": "wait_approval",
            "approval_summary": (
                f"退款申请：订单 {order_id}，金额 {state.get('action_draft', {}).get('amount', '?')} 元"
                f"（金额由领域政策计算），操作 {op_id}，审批编号 {approval_id}"
            ),
            "audit_event_ids": state.get("audit_event_ids", []) + gateway.collect_audit_events(tenant),
        }

    def request_approval(state: AgentState) -> dict:
        """高风险动作草稿落库后挂起，等待人工审批（interrupt）。"""
        interrupt({
            "type": APPROVAL_INTERRUPT_TYPE,
            "approval_id": state.get("approval_id"),
            "operation_id": state.get("operation_id"),
            "ticket_id": state.get("ticket_id"),
            "risk_level": state.get("risk_level", "high"),
            "action_draft": state.get("action_draft"),
            "summary": state.get("approval_summary"),
        })
        return {"next_action": "wait_approval"}

    # ---------- 恢复：重读领域事实源中的审批决定 ----------

    def apply_decision(state: AgentState) -> dict:
        """恢复后重读领域事实源中的审批决定（只读，不写）。

        - APPROVED → 受控执行；REJECTED → 关单定性拒绝（无副作用）；
        - EXECUTED/FAILED/UNKNOWN → 幂等收尾，不重复执行；
        - PENDING（审批未提交）→ 忽略任何 resume 携带的决定，再次 interrupt 等待真实审批；
          每次 resume 都回到本循环重新读领域决定，直到终审。
        """
        op_id = state.get("operation_id")
        tenant = state.get("tenant_id")
        while True:
            if not op_id:
                return {"error_code": "OPERATION_NOT_FOUND", "outcome": "error",
                        "next_action": "escalate", "reply": "恢复失败：缺少操作编号"}
            try:
                op = gateway.get_operation(tenant, op_id)  # 只读，领域服务为事实源
            except AfterSalesError as e:
                return {"error_code": e.code.value, "outcome": "error",
                        "next_action": "escalate",
                        "reply": f"恢复失败：操作不存在或不可读（{e.message}），未执行任何副作用"}
            if op.status == OperationStatus.APPROVED:
                return {"next_action": "execute_operation", "decision": "approved"}
            if op.status == OperationStatus.REJECTED:
                return {"next_action": "settle_rejected", "decision": "rejected"}
            if op.status == OperationStatus.EXECUTED:
                return {"outcome": "already_executed", "next_action": "finished",
                        "reply": "该操作此前已执行完成，重复恢复不重复执行（幂等）"}
            if op.status == OperationStatus.FAILED:
                return {"outcome": "failed", "next_action": "finished",
                        "reply": "该操作已对账失败并终态，重复恢复不重复执行"}
            if op.status == OperationStatus.UNKNOWN:
                return {"outcome": "operation_unknown", "next_action": "reconcile_required",
                        "reply": "外部执行结果未知，操作处于 operation_unknown；只能以原操作编号查询/对账，禁止换键重试"}
            # PENDING_APPROVAL 等未决：审批决定尚未提交 → 不执行任何副作用，
            # 再次挂起等待真实审批（resume 携带的 decision 不生效）
            interrupt({
                "type": APPROVAL_INTERRUPT_TYPE,
                "waiting": True,
                "approval_id": state.get("approval_id"),
                "operation_id": op_id,
                "reason": "审批决定尚未提交到领域服务；已忽略本次恢复请求，继续等待真实审批",
            })

    # ---------- 执行 / 收尾 ----------

    def execute_operation(state: AgentState) -> dict:
        sim = state.get("simulate_external", "success")
        tenant = state.get("tenant_id")
        try:
            op = gateway.execute(tenant, state["operation_id"], external_result=sim)
        except AfterSalesError as e:
            return {"error_code": e.code.value, "outcome": "error",
                    "next_action": "escalate", "reply": f"执行失败（{e.message}）"}
        if op.status == OperationStatus.UNKNOWN:
            return {"outcome": "operation_unknown", "next_action": "reconcile_required",
                    "reply": "外部执行结果未知：操作进入 operation_unknown；只能以原操作编号查询/对账"}
        # EXECUTED：受控关单（无未决操作守卫在领域服务）
        try:
            gateway.close_ticket(tenant, state["ticket_id"])
        except AfterSalesError as e:
            return {"error_code": e.code.value, "outcome": "error",
                    "next_action": "escalate", "reply": f"退款已执行但关单失败（{e.message}），需人工介入"}
        return {"outcome": "refunded", "next_action": "finished",
                "reply": "退款已执行（模拟），工单已按已退款关闭",
                "audit_event_ids": state.get("audit_event_ids", []) + gateway.collect_audit_events(tenant)}

    def settle_rejected(state: AgentState) -> dict:
        tenant = state.get("tenant_id")
        try:
            gateway.close_ticket(tenant, state["ticket_id"])
        except AfterSalesError as e:
            return {"error_code": e.code.value, "outcome": "error",
                    "next_action": "escalate", "reply": f"关单失败（{e.message}）"}
        return {"outcome": "rejected", "next_action": "finished",
                "reply": "审批拒绝：未执行任何退款/副作用，工单已按拒绝关闭",
                "audit_event_ids": state.get("audit_event_ids", []) + gateway.collect_audit_events(tenant)}

    def escalate(state: AgentState) -> dict:
        code = state.get("error_code") or "NO_APPLICABLE_HANDLER"
        return {"outcome": "escalated", "next_action": "escalated",
                "reply": state.get("reply") or f"已转人工处理（原因码 {code}）；Agent 未执行任何写操作"}

    return {
        "parse": parse,
        "clarify": clarify,
        "gather_evidence": gather_evidence,
        "plan": plan,
        "create_ticket_draft": create_ticket_draft,
        "request_approval": request_approval,
        "apply_decision": apply_decision,
        "execute_operation": execute_operation,
        "settle_rejected": settle_rejected,
        "escalate": escalate,
    }


# ---------- 条件路由（纯函数，可单测） ----------

def route_after_parse(state: AgentState) -> str:
    """parse 之后路由：缺参 → 澄清；无法识别/不支持动作 → 转人工；退款 → 证据编排。"""
    if state.get("missing_fields"):
        return "clarify"
    intent = state.get("intent")
    if intent in ("unknown", "other") or intent is None:
        return "escalate"
    if intent in _UNSUPPORTED_DRAFT_INTENTS:
        return "escalate"
    if intent == "refund":
        return "gather_evidence"
    return "escalate"


def route_after_gather(state: AgentState) -> str:
    return "escalate" if state.get("error_code") else "plan"


def route_after_draft(state: AgentState) -> str:
    """草稿落库后：待审批 → request_approval；重复请求已终态（finished）→ 直接 END。"""
    return "request_approval" if state.get("next_action") == "wait_approval" else "end"


def route_after_plan(state: AgentState) -> str:
    return "escalate" if state.get("error_code") else "create_ticket_draft"


def route_after_decision(state: AgentState) -> str:
    """apply_decision 之后：next_action 即目标节点；finished/unknown 直接收尾。"""
    return state.get("next_action") or "escalate"


def terminal_route(state: AgentState) -> str:
    """finished / escalate / reconcile_required / escalated 均到 END（无更多副作用）。"""
    return "end"
