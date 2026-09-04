"""Agent 工作流状态 Schema（阶段 2，LangGraph 单 Agent）。

checkpoint 只保存流程恢复状态，不保存业务最终真相（AGENTS.md 第五条）：
工单 / 操作 / 审批 / 执行 / 审计的最终事实一律以确定性领域服务为准。
"""
from __future__ import annotations

from typing import Optional, TypedDict


class AgentState(TypedDict, total=False):
    """单 Agent 售后工作流状态。

    必含字段（阶段 2 要求）：tenant_id / thread_id / ticket_id / intent /
    missing_fields / evidence_refs / action_draft / risk_level / approval_id /
    operation_id / next_action / error_code / audit_event_ids。
    其余为流程运行字段。
    """

    # ---- 必含字段 ----
    tenant_id: str
    thread_id: str
    ticket_id: Optional[str]
    intent: Optional[str]            # refund | return | exchange | replace | upgrade | unknown | other
    missing_fields: list[str]        # 缺参列表（如 ["order_id"]、["reason"]）
    evidence_refs: list[str]         # 证据引用（如 "order:ORD-1"、"history:..."）
    action_draft: Optional[dict]     # 动作草稿解释（仅展示用途，金额由领域服务决定）
    risk_level: Optional[str]        # low | high
    approval_id: Optional[str]       # 独立于 operation_id 的审批编号
    operation_id: Optional[str]
    next_action: Optional[str]       # awaiting_input | escalate | wait_approval | execute |
                                     # reconcile_required | finished
    error_code: Optional[str]        # 领域错误码（AFTER_SALES_*）或流程错误（INVALID_RESUME 等）
    audit_event_ids: list[str]       # 本流程触发过的领域审计事件 id（"audit-N:action:entity"）

    # ---- 流程运行字段 ----
    user_request: str                # 用户本轮输入
    order_id: Optional[str]
    reason_tags: list[str]           # 结构化诉求标签（"damaged"/"missing_item"…）
    order_summary: Optional[dict]    # 只读证据快照（非事实源，仅展示）
    questions: Optional[list[str]]   # 澄清问题（clarify interrupt 输出）
    decision: Optional[str]          # resume 携带（仅供参考，不作为业务依据）
    outcome: Optional[str]           # refunded | rejected | escalated | operation_unknown |
                                     # already_executed | cancelled | no_action
    simulate_external: str           # success | timeout（仅测试/演示模拟外部执行结果）
    reply: Optional[str]             # 面向用户的最终话术（V1 由模板生成，非 LLM）
    approval_summary: Optional[str]  # 展示给审批人的可读摘要

    # 线程生命周期（任务卡 J）：请求指纹（tenant + 规范化请求哈希），
    # checkpoint 中不可变——同一 thread 只能继续原请求。
    thread_request_fingerprint: Optional[dict]


def new_state() -> AgentState:
    """流程状态初始值（列表/字典字段默认）。"""
    return AgentState(
        missing_fields=[],
        evidence_refs=[],
        audit_event_ids=[],
        reason_tags=[],
        simulate_external="success",
    )


APPROVAL_INTERRUPT_TYPE = "approval"
CLARIFY_INTERRUPT_TYPE = "clarify"
