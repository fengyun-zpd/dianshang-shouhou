"""确定性领域服务的受控网关（阶段 2 Agent 编排唯一通道）。

本网关只依赖 AfterSalesApplicationPort（MemoryAdapter / PgCommandAdapter 均可，
不 isinstance 分支）——所有领域调用一律 tenant-first，租户门禁由后端实施。

约束（AGENTS.md 第三、四、六条）：
- 本网关**不做任何业务判断**：金额、政策、权限、状态、幂等全部由领域服务裁决；
- 写命令角色固定：建单 / 提交 / 关单 = Role.AGENT，执行 / 对账 = Role.SYSTEM；
  网关不暴露 approver 写通道给 Agent → Agent 无法伪造审批
  （submit_approver_decision 仅供授权人员/上层运行器调用）；
- 只读查询：订单、历史工单（租户内）、物流（当前未接入 → 显式不可用）；
- collect_audit_events() 返回自上次水位以来的领域审计事件 id（用于 audit_event_ids）。

工具去重（可选 ledger）：受控写工具按 (工具名, 租户, thread_id, 参数摘要) 去重，
重复调用复用首次结果；`get_operation` 等事实重读工具**永不缓存**。账本只是第二道防线，
重复副作用的最终兜底仍是领域服务幂等键。
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

from src.domain.after_sales import (
    AfterSalesTicket,
    ApproveCommand,
    CloseTicketCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    Operation,
    OperationStatus,
    Order,
    ReconcileCommand,
    RejectCommand,
    RefundPlan,
    RequestType,
    Role,
    SubmitCommand,
)
from src.domain.after_sales.ports import AfterSalesApplicationPort

from .tool_ledger import NEVER_DEDUPED_TOOLS, ToolCallLedger, current_tool_context


class AfterSalesGateway:
    """包住 AfterSalesApplicationPort 的受控通道（进程内实现，不绑定后端类型）。"""

    def __init__(self, port: AfterSalesApplicationPort,
                 ledger: Optional[ToolCallLedger] = None):
        self._port = port
        self._audit_watermark = 0  # 已收集到的领域审计事件条数（水位，按租户过滤视图）
        self._ledger = ledger

    # ---------- 工具去重（第二道防线；领域幂等键仍是最终兜底） ----------

    def _dedup(self, tool: str, tenant_id: Optional[str], args: dict,
               fn: Callable[[], Any]) -> Any:
        """受控写工具去重：命中重复键复用首次结果，不发起第二次领域调用。

        以下情况直接穿透（不去重）：
        - 未装配账本（向后兼容）；
        - 工具在 NEVER_DEDUPED_TOOLS（审批/操作事实重读必须每次读事实源）；
        - 无节点执行上下文（脚本直调网关，不假设线程语义）。
        """
        ledger = self._ledger
        if ledger is None or tool in NEVER_DEDUPED_TOOLS:
            return fn()
        ctx = current_tool_context()
        if ctx is None or not ctx.thread_id:
            return fn()
        return ledger.invoke(tool, tenant_id, ctx.thread_id, args, fn)

    @property
    def tool_ledger(self) -> Optional[ToolCallLedger]:
        return self._ledger

    # ---------- 只读查询（证据编排） ----------

    def get_order(self, tenant_id: str, order_id: str) -> Order:
        return self._port.get_order(tenant_id, order_id)

    def get_ticket_for(self, tenant_id: str, ticket_id: str) -> AfterSalesTicket:
        """租户内只读工单查询：跨租户/不存在由后端门禁拒绝（TENANT_MISMATCH/NOT_FOUND）。"""
        return self._port.get_ticket(tenant_id, ticket_id)

    def customer_id_of_order(self, tenant_id: str, order_id: str) -> str:
        return self.get_order(tenant_id, order_id).customer_id

    def history_tickets(self, tenant_id: str, customer_id: str) -> list[AfterSalesTicket]:
        """该客户的历史工单（只读证据）。"""
        return self._port.list_customer_tickets(tenant_id, customer_id)

    def logistics_status(self, tenant_id: str, order_id: str) -> dict:
        """物流查询：V1 未接入，显式返回不可用（证据缺省，不虚构）。"""
        return {"available": False, "tenant_id": tenant_id, "order_id": order_id,
                "reason": "物流查询未接入（规划中，阶段 3+）"}

    def compute_refund_plan(
        self, tenant_id: str, order_id: str, reason_tags: Tuple[str, ...],
    ) -> RefundPlan:
        """确定性退款金额（金额唯一来源，Agent 不自行决定）。"""
        return self._port.compute_refund_plan(tenant_id, order_id, RequestType.REFUND, reason_tags)

    # ---------- 受控写（角色固定） ----------

    def create_ticket(
        self, tenant_id: str, order_id: str, customer_id: str,
        reason: str, reason_tags: Tuple[str, ...], idempotency_key: str,
    ) -> AfterSalesTicket:
        return self._dedup(
            "create_ticket", tenant_id,
            {"order_id": order_id, "customer_id": customer_id, "reason": reason,
             "reason_tags": list(reason_tags), "idempotency_key": idempotency_key},
            lambda: self._port.create_ticket(CreateTicketCommand(
                tenant_id=tenant_id, order_id=order_id, customer_id=customer_id,
                request_type=RequestType.REFUND, reason=reason, reason_tags=reason_tags,
                actor=Role.AGENT, idempotency_key=idempotency_key,
            )),
        )

    def create_refund(self, tenant_id: str, ticket_id: str, amount,
                      reason_detail: str, idempotency_key: str) -> Operation:
        return self._dedup(
            "create_refund", tenant_id,
            {"ticket_id": ticket_id, "amount": str(amount),
             "reason_detail": reason_detail, "idempotency_key": idempotency_key},
            lambda: self._port.create_refund_draft(tenant_id, CreateRefundCommand(
                ticket_id=ticket_id, amount=amount, reason_detail=reason_detail,
                actor=Role.AGENT, idempotency_key=idempotency_key,
            )),
        )

    def submit(self, tenant_id: str, operation_id: str) -> Operation:
        """提交审批（幂等）：DRAFT → PENDING_APPROVAL；已提交或已终态则原样返回。

        幂等化位于网关层（编排语义），不改动领域服务的严格状态机。
        """
        def _do() -> Operation:
            op = self._port.get_operation(tenant_id, operation_id)
            if op.status == OperationStatus.DRAFT:
                return self._port.submit(tenant_id, SubmitCommand(
                    operation_id=operation_id, actor=Role.AGENT))
            return op

        return self._dedup("submit", tenant_id, {"operation_id": operation_id}, _do)

    def close_ticket(self, tenant_id: str, ticket_id: str) -> AfterSalesTicket:
        return self._dedup(
            "close_ticket", tenant_id, {"ticket_id": ticket_id},
            lambda: self._port.close_ticket(tenant_id, CloseTicketCommand(
                ticket_id=ticket_id, actor=Role.AGENT)),
        )

    def execute(self, tenant_id: str, operation_id: str,
                external_result: str = "success") -> Operation:
        return self._dedup(
            "execute", tenant_id,
            {"operation_id": operation_id, "external_result": external_result},
            lambda: self._port.execute(tenant_id, ExecuteCommand(
                operation_id=operation_id, actor=Role.SYSTEM,
                external_result=external_result)),
        )

    def reconcile(self, tenant_id: str, operation_id: str, result: str) -> Operation:
        """operation_unknown 对账收口（SYSTEM；只能以原 operation_id）。"""
        return self._dedup(
            "reconcile", tenant_id,
            {"operation_id": operation_id, "result": result},
            lambda: self._port.reconcile(tenant_id, ReconcileCommand(
                operation_id=operation_id, actor=Role.SYSTEM, result=result)),
        )

    # ---------- 审批（由外部授权人员提交决定到事实源；Agent 无此通道） ----------

    def get_operation(self, tenant_id: str, operation_id: str) -> Operation:
        return self._port.get_operation(tenant_id, operation_id)

    def submit_approver_decision(self, tenant_id: str, operation_id: str,
                                 decision: str, reason: Optional[str] = None) -> Operation:
        """仅 APPROVER 可调用：向领域事实源提交审批决定（approve/reject）。

        版本号由领域服务校验（读当前版本）；这是"已提交且带版本号的决定"。
        """
        op = self._port.get_operation(tenant_id, operation_id)
        if decision == "approved":
            return self._port.approve(tenant_id, ApproveCommand(
                operation_id=operation_id, actor=Role.APPROVER, decision_version=op.version,
            ))
        if decision == "rejected":
            return self._port.reject(tenant_id, RejectCommand(
                operation_id=operation_id, actor=Role.APPROVER,
                reason=reason or "审批人拒绝", decision_version=op.version,
            ))
        raise ValueError(f"非法审批决定：{decision!r}")

    # ---------- 审计水位 ----------

    def collect_audit_events(self, tenant_id: str) -> list[str]:
        """返回自上次水位以来的领域审计事件 id（幂等：重复调用不重复计数）。

        水位作用于租户过滤视图（port.audit_log(tenant_id)）：他租户新增事件不进本
        租户视图，不影响本租户水位推进。V1 单租户/单流程演示与既有语义等价。
        """
        log = self._port.audit_log(tenant_id)
        fresh = log[self._audit_watermark:]
        ids = [
            f"audit-{self._audit_watermark + i}:{e.action}:{e.entity_id}"
            for i, e in enumerate(fresh)
        ]
        self._audit_watermark = len(log)
        return ids
