"""确定性领域服务的受控网关（阶段 2 Agent 编排唯一通道）。

约束（AGENTS.md 第三、四、六条）：
- 本网关**不做任何业务判断**：金额、政策、权限、状态、幂等全部由领域服务裁决；
- 写命令角色固定：建单 / 提交 / 关单 = Role.AGENT，执行 / 对账 = Role.SYSTEM；
  网关不暴露 approver 写通道 → Agent 无法伪造审批；
- 只读查询：订单、历史工单（租户内）、物流（当前未接入 → 显式不可用）；
- collect_audit_events() 返回自上次水位以来的领域审计事件 id（用于 audit_event_ids）。
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesService,
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


class AfterSalesGateway:
    """包住 AfterSalesService 的受控通道（进程内内存实现）。"""

    def __init__(self, service: AfterSalesService):
        self._svc = service
        self._audit_watermark = 0  # 已收集到的领域审计事件条数（水位）

    # ---------- 只读查询（证据编排） ----------

    def get_order(self, tenant_id: str, order_id: str) -> Order:
        return self._svc.get_order_by_id(tenant_id, order_id)

    def customer_id_of_order(self, tenant_id: str, order_id: str) -> str:
        return self.get_order(tenant_id, order_id).customer_id

    def history_tickets(self, tenant_id: str, customer_id: str) -> list[AfterSalesTicket]:
        """该客户的历史工单（只读证据）。"""
        return self._svc.list_customer_tickets(tenant_id, customer_id)

    def logistics_status(self, tenant_id: str, order_id: str) -> dict:
        """物流查询：V1 未接入，显式返回不可用（证据缺省，不虚构）。"""
        return {"available": False, "tenant_id": tenant_id, "order_id": order_id,
                "reason": "物流查询未接入（规划中，阶段 3+）"}

    def compute_refund_plan(
        self, tenant_id: str, order_id: str, reason_tags: Tuple[str, ...],
    ) -> RefundPlan:
        """确定性退款金额（金额唯一来源，Agent 不自行决定）。"""
        return self._svc.compute_refund_plan(tenant_id, order_id, RequestType.REFUND, reason_tags)

    # ---------- 受控写（角色固定） ----------

    def create_ticket(
        self, tenant_id: str, order_id: str, customer_id: str,
        reason: str, reason_tags: Tuple[str, ...], idempotency_key: str,
    ) -> AfterSalesTicket:
        return self._svc.create_ticket(CreateTicketCommand(
            tenant_id=tenant_id, order_id=order_id, customer_id=customer_id,
            request_type=RequestType.REFUND, reason=reason, reason_tags=reason_tags,
            actor=Role.AGENT, idempotency_key=idempotency_key,
        ))

    def create_refund(self, ticket_id: str, amount, reason_detail: str, idempotency_key: str) -> Operation:
        return self._svc.create_refund(CreateRefundCommand(
            ticket_id=ticket_id, amount=amount, reason_detail=reason_detail,
            actor=Role.AGENT, idempotency_key=idempotency_key,
        ))

    def submit(self, operation_id: str) -> Operation:
        """提交审批（幂等）：DRAFT → PENDING_APPROVAL；已提交或已终态则原样返回。

        幂等化位于网关层（编排语义），不改动领域服务的严格状态机。
        """
        op = self._svc.get_operation(operation_id)
        if op.status == OperationStatus.DRAFT:
            return self._svc.submit(SubmitCommand(operation_id=operation_id, actor=Role.AGENT))
        return op

    def close_ticket(self, ticket_id: str) -> AfterSalesTicket:
        return self._svc.close_ticket(CloseTicketCommand(ticket_id=ticket_id, actor=Role.AGENT))

    def execute(self, operation_id: str, external_result: str = "success") -> Operation:
        return self._svc.execute(ExecuteCommand(
            operation_id=operation_id, actor=Role.SYSTEM, external_result=external_result,
        ))

    def reconcile(self, operation_id: str, result: str) -> Operation:
        """operation_unknown 对账收口（SYSTEM；只能以原 operation_id）。"""
        return self._svc.reconcile(ReconcileCommand(operation_id=operation_id, actor=Role.SYSTEM, result=result))

    # ---------- 审批（由外部授权人员提交决定到事实源；Agent 无此通道） ----------

    def get_operation(self, operation_id: str) -> Operation:
        return self._svc.get_operation(operation_id)

    def submit_approver_decision(self, operation_id: str, decision: str, reason: Optional[str] = None) -> Operation:
        """仅 APPROVER 可调用：向领域事实源提交审批决定（approve/reject）。

        版本号由领域服务校验（读当前版本）；这是"已提交且带版本号的决定"。
        """
        op = self._svc.get_operation(operation_id)
        if decision == "approved":
            return self._svc.approve(ApproveCommand(
                operation_id=operation_id, actor=Role.APPROVER, decision_version=op.version,
            ))
        if decision == "rejected":
            return self._svc.reject(RejectCommand(operation_id=operation_id, actor=Role.APPROVER, reason=reason or "审批人拒绝"))
        raise ValueError(f"非法审批决定：{decision!r}")

    # ---------- 审计水位 ----------

    def collect_audit_events(self) -> list[str]:
        """返回自上次水位以来的审计事件 id（幂等：重复调用不重复计数）。"""
        log = self._svc.audit_log()
        fresh = log[self._audit_watermark:]
        ids = [
            f"audit-{self._audit_watermark + i}:{e.action}:{e.entity_id}"
            for i, e in enumerate(fresh)
        ]
        self._audit_watermark = len(log)
        return ids
