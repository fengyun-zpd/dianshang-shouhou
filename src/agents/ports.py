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
"""
from __future__ import annotations

from typing import Optional, Tuple

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


class AfterSalesGateway:
    """包住 AfterSalesApplicationPort 的受控通道（进程内实现，不绑定后端类型）。"""

    def __init__(self, port: AfterSalesApplicationPort):
        self._port = port
        self._audit_watermark = 0  # 已收集到的领域审计事件条数（水位，按租户过滤视图）

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
        return self._port.create_ticket(CreateTicketCommand(
            tenant_id=tenant_id, order_id=order_id, customer_id=customer_id,
            request_type=RequestType.REFUND, reason=reason, reason_tags=reason_tags,
            actor=Role.AGENT, idempotency_key=idempotency_key,
        ))

    def create_refund(self, tenant_id: str, ticket_id: str, amount,
                      reason_detail: str, idempotency_key: str) -> Operation:
        return self._port.create_refund_draft(tenant_id, CreateRefundCommand(
            ticket_id=ticket_id, amount=amount, reason_detail=reason_detail,
            actor=Role.AGENT, idempotency_key=idempotency_key,
        ))

    def submit(self, tenant_id: str, operation_id: str) -> Operation:
        """提交审批（幂等）：DRAFT → PENDING_APPROVAL；已提交或已终态则原样返回。

        幂等化位于网关层（编排语义），不改动领域服务的严格状态机。
        """
        op = self._port.get_operation(tenant_id, operation_id)
        if op.status == OperationStatus.DRAFT:
            return self._port.submit(tenant_id,
                                     SubmitCommand(operation_id=operation_id, actor=Role.AGENT))
        return op

    def close_ticket(self, tenant_id: str, ticket_id: str) -> AfterSalesTicket:
        return self._port.close_ticket(tenant_id,
                                       CloseTicketCommand(ticket_id=ticket_id, actor=Role.AGENT))

    def execute(self, tenant_id: str, operation_id: str,
                external_result: str = "success") -> Operation:
        return self._port.execute(tenant_id, ExecuteCommand(
            operation_id=operation_id, actor=Role.SYSTEM, external_result=external_result,
        ))

    def reconcile(self, tenant_id: str, operation_id: str, result: str) -> Operation:
        """operation_unknown 对账收口（SYSTEM；只能以原 operation_id）。"""
        return self._port.reconcile(tenant_id, ReconcileCommand(
            operation_id=operation_id, actor=Role.SYSTEM, result=result))

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
