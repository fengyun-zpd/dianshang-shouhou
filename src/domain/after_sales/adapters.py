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


class PgServiceFacade:
    """pg profile 的 API 后端鸭子实现（最小证据子集，供 PG profile HTTP e2e）。

    方法面 = FastAPI 路由实际调用的内存 AfterSalesService 子集（命令经 PgCommandAdapter →
    PgCommandService 单事务；只读经 Repository 行 → domain）。未覆盖端点所需方法抛
    NotImplementedError——完整 API 面接入 pg 为进行中（不静默声称全端点可用）。
    """

    def __init__(self, commands: PgCommandService, repo: AfterSalesRepository):
        self._adapter = PgCommandAdapter(commands, repo)
        self._repo = repo

    # ---------- 命令（桥接端口；tenant 从实体解析） ----------
    def create_ticket(self, cmd: CreateTicketCommand) -> AfterSalesTicket:
        return self._adapter.create_ticket(cmd)

    def create_refund(self, cmd: CreateRefundCommand) -> Operation:
        t = self._ticket_tenant(cmd.ticket_id)
        return self._adapter.create_refund_draft(t, cmd)

    def submit(self, cmd: SubmitCommand) -> Operation:
        return self._adapter.submit(self._operation_tenant(cmd.operation_id), cmd)

    def approve(self, cmd: ApproveCommand, decided_by: Optional[str] = None) -> Operation:
        return self._adapter.approve(self._operation_tenant(cmd.operation_id), cmd,
                                     decided_by=decided_by)

    def reject(self, cmd: RejectCommand, decided_by: Optional[str] = None) -> Operation:
        return self._adapter.reject(self._operation_tenant(cmd.operation_id), cmd,
                                    decided_by=decided_by)

    def execute(self, cmd: ExecuteCommand) -> Operation:
        return self._adapter.execute(self._operation_tenant(cmd.operation_id), cmd)

    def reconcile(self, cmd: ReconcileCommand) -> Operation:
        return self._adapter.reconcile(self._operation_tenant(cmd.operation_id), cmd)

    def close_ticket(self, cmd: CloseTicketCommand) -> AfterSalesTicket:
        return self._adapter.close_ticket(self._ticket_tenant(cmd.ticket_id), cmd)

    # ---------- 只读（API 子集；id 全局唯一假设 → 扫行定位租户） ----------
    def get_ticket(self, ticket_id: str) -> AfterSalesTicket:
        for r in self._repo.list_tickets():
            if r.ticket_id == ticket_id:
                return ticket_from_row(r)
        raise AfterSalesError(AfterSalesErrorCode.TICKET_NOT_FOUND,
                              f"工单 {ticket_id} 不存在")

    def get_operation(self, operation_id: str) -> Operation:
        for r in self._repo.list_operations():
            if r.operation_id == operation_id:
                return operation_from_row(r)
        raise AfterSalesError(AfterSalesErrorCode.OPERATION_NOT_FOUND,
                              f"操作 {operation_id} 不存在")

    def get_order_by_id(self, tenant_id: str, order_id: str):
        from src.persistence.pg_backed import order_from_row
        row = self._repo.get_order(tenant_id, order_id)
        if row is None:
            raise AfterSalesError(AfterSalesErrorCode.ORDER_NOT_FOUND, "订单不存在")
        return order_from_row(row)

    def _operation_tenant(self, operation_id: str) -> str:
        for r in self._repo.list_operations():
            if r.operation_id == operation_id:
                return r.tenant_id
        raise AfterSalesError(AfterSalesErrorCode.OPERATION_NOT_FOUND,
                              f"操作 {operation_id} 不存在")

    def _ticket_tenant(self, ticket_id: str) -> str:
        for r in self._repo.list_tickets():
            if r.ticket_id == ticket_id:
                return r.tenant_id
        raise AfterSalesError(AfterSalesErrorCode.TICKET_NOT_FOUND,
                              f"工单 {ticket_id} 不存在")
