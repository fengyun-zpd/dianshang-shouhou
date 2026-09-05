"""AfterSalesApplicationPort 的双后端 Adapter（阶段四第 3 节）。

- MemoryAdapter：包装内存 AfterSalesService（演示/测试后端；生产不得使用）；
- PgCommandAdapter：包装 PgCommandService + Repository（pg profile 生产命令路径，
  单事务事实；approve/reject 以 decided_by=认证 principal 落库）。
两者实现同一端口；调用方不区分后端类型（不 isinstance）。
"""
from __future__ import annotations

from typing import Optional

from src.domain.after_sales import AfterSalesService
from src.domain.after_sales.models import (
    AfterSalesError,
    AfterSalesErrorCode,
    AfterSalesTicket,
    ApproveCommand,
    CloseTicketCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    Operation,
    ReconcileCommand,
    RejectCommand,
    SubmitCommand,
)
from src.domain.after_sales.pg_commands import PgCommandService
from src.persistence.pg_backed import operation_from_row, ticket_from_row
from src.repo import AfterSalesRepository

from .ports import AfterSalesApplicationPort


class MemoryAdapter:
    """内存后端适配器：只用于本地演示/测试。approve/reject 由内存领域版本 CAS 校验。"""

    def __init__(self, service: AfterSalesService):
        self._svc = service

    def create_ticket(self, cmd: CreateTicketCommand) -> AfterSalesTicket:
        return self._svc.create_ticket(cmd)

    def create_refund_draft(self, tenant_id: str, cmd: CreateRefundCommand) -> Operation:
        return self._svc.create_refund(cmd)

    def submit(self, tenant_id: str, cmd: SubmitCommand) -> Operation:
        return self._svc.submit(cmd)

    def approve(self, tenant_id: str, cmd: ApproveCommand,
                decided_by: Optional[str] = None) -> Operation:
        return self._svc.approve(cmd)

    def reject(self, tenant_id: str, cmd: RejectCommand,
               decided_by: Optional[str] = None) -> Operation:
        return self._svc.reject(cmd)

    def execute(self, tenant_id: str, cmd: ExecuteCommand) -> Operation:
        return self._svc.execute(cmd)

    def reconcile(self, tenant_id: str, cmd: ReconcileCommand) -> Operation:
        return self._svc.reconcile(cmd)

    def close_ticket(self, tenant_id: str, cmd: CloseTicketCommand) -> AfterSalesTicket:
        return self._svc.close_ticket(cmd)

    def get_ticket(self, tenant_id: str, ticket_id: str) -> AfterSalesTicket:
        t = self._svc.get_ticket(ticket_id)
        if t.tenant_id != tenant_id:
            raise AfterSalesError(AfterSalesErrorCode.TENANT_MISMATCH,
                                  f"工单 {ticket_id} 不属于租户 {tenant_id}")
        return t

    def get_operation(self, tenant_id: str, operation_id: str) -> Operation:
        op = self._svc.get_operation(operation_id)
        if op.tenant_id != tenant_id:
            raise AfterSalesError(AfterSalesErrorCode.TENANT_MISMATCH,
                                  f"操作 {operation_id} 不属于租户 {tenant_id}")
        return op


class PgCommandAdapter:
    """PG-first 命令后端适配器（pg profile）：单事务事实 + 授权人 principal。"""

    def __init__(self, commands: PgCommandService, repo: AfterSalesRepository):
        self._cmd = commands
        self._repo = repo

    def create_ticket(self, cmd: CreateTicketCommand) -> AfterSalesTicket:
        return self._cmd.create_ticket(cmd)

    def create_refund_draft(self, tenant_id: str, cmd: CreateRefundCommand) -> Operation:
        return self._cmd.create_refund_draft(tenant_id, cmd)

    def submit(self, tenant_id: str, cmd: SubmitCommand) -> Operation:
        return self._cmd.submit(tenant_id, cmd)

    def approve(self, tenant_id: str, cmd: ApproveCommand,
                decided_by: Optional[str] = None) -> Operation:
        return self._cmd.approve(tenant_id, cmd, decided_by=decided_by)

    def reject(self, tenant_id: str, cmd: RejectCommand,
               decided_by: Optional[str] = None) -> Operation:
        return self._cmd.reject(tenant_id, cmd, decided_by=decided_by)

    def execute(self, tenant_id: str, cmd: ExecuteCommand) -> Operation:
        return self._cmd.execute(tenant_id, cmd)

    def reconcile(self, tenant_id: str, cmd: ReconcileCommand) -> Operation:
        return self._cmd.reconcile(tenant_id, cmd)

    def close_ticket(self, tenant_id: str, cmd: CloseTicketCommand) -> AfterSalesTicket:
        return self._cmd.close_ticket(tenant_id, cmd)

    def get_ticket(self, tenant_id: str, ticket_id: str) -> AfterSalesTicket:
        row = self._repo.get_ticket(tenant_id, ticket_id)
        if row is None:
            raise AfterSalesError(AfterSalesErrorCode.TICKET_NOT_FOUND,
                                  f"工单 {ticket_id} 不存在")
        return ticket_from_row(row)

    def get_operation(self, tenant_id: str, operation_id: str) -> Operation:
        row = self._repo.get_operation(tenant_id, operation_id)
        if row is None:
            raise AfterSalesError(AfterSalesErrorCode.OPERATION_NOT_FOUND,
                                  f"操作 {operation_id} 不存在")
        return operation_from_row(row)


__all__ = ["AfterSalesApplicationPort", "MemoryAdapter", "PgCommandAdapter"]
