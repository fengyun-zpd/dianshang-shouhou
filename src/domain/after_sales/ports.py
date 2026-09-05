"""AfterSalesApplicationPort（阶段四第 3 节）：API / Gateway / Runner 只依赖的读写端口。

内存 AfterSalesService（演示/测试）与 PgCommandService（pg profile 生产命令路径）
通过 adapter 实现同一端口；业务节点**不得以 isinstance 判断后端**。
所有写命令显式带 tenant_id（权限来源）；approve/reject 的授权人以 decided_by(principal)
显式传入（Agent 永不能代表审批人——角色门禁由后端实现）。
"""
from __future__ import annotations

from typing import Protocol, Optional

from src.domain.after_sales.models import (
    AfterSalesTicket,
    ApproveCommand,
    CloseTicketCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    Operation,
    ReconcileCommand,
    RefundPlan,
    RejectCommand,
    RequestType,
    SubmitCommand,
)


class AfterSalesApplicationPort(Protocol):
    """业务应用端口：写命令（单事务事实）+ tenant-first 只读。"""

    # ---------- 命令 ----------
    def create_ticket(self, cmd: CreateTicketCommand) -> AfterSalesTicket: ...
    def create_refund_draft(self, tenant_id: str, cmd: CreateRefundCommand) -> Operation: ...
    def submit(self, tenant_id: str, cmd: SubmitCommand) -> Operation: ...
    def approve(self, tenant_id: str, cmd: ApproveCommand,
                decided_by: Optional[str] = None) -> Operation: ...
    def reject(self, tenant_id: str, cmd: RejectCommand,
               decided_by: Optional[str] = None) -> Operation: ...
    def execute(self, tenant_id: str, cmd: ExecuteCommand) -> Operation: ...
    def reconcile(self, tenant_id: str, cmd: ReconcileCommand) -> Operation: ...
    def close_ticket(self, tenant_id: str, cmd: CloseTicketCommand) -> AfterSalesTicket: ...

    # ---------- 只读（全部 tenant-first：禁止裸 ID 全表扫描） ----------
    def get_ticket(self, tenant_id: str, ticket_id: str) -> AfterSalesTicket: ...
    def get_operation(self, tenant_id: str, operation_id: str) -> Operation: ...
    def list_operations(self, tenant_id: str, ticket_id: str) -> list[Operation]:
        """工单下的操作列表（tenant-first：工单不存在/跨租户同样拒绝）。"""
        ...
    def get_order(self, tenant_id: str, order_id: str): ...
    def list_customer_tickets(self, tenant_id: str, customer_id: str) -> list[AfterSalesTicket]: ...
    def compute_refund_plan(self, tenant_id: str, order_id: str,
                            request_type: RequestType, reason_tags) -> RefundPlan: ...
    def audit_log(self, tenant_id: str): ...
