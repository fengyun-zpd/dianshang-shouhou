"""内存 Repository（K4 契约实现的参考/测试用；非生产事实源）。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from typing import Iterator, Optional

from .interfaces import (
    AfterSalesRepository,
    ApprovalRow,
    AuditRow,
    IdemRow,
    OperationRow,
    OptimisticLockError,
    OrderRow,
    TicketRow,
    UniqueViolation,
)


class MemoryAfterSalesRepository(AfterSalesRepository):
    """契约的内存实现：模拟唯一约束、乐观版本与订单行锁语义。"""

    def __init__(self) -> None:
        self._orders: dict[tuple[str, str], OrderRow] = {}
        self._tickets: dict[tuple[str, str], TicketRow] = {}
        self._operations: dict[tuple[str, str], OperationRow] = {}
        self._approvals: list[ApprovalRow] = []
        self._audits: list[AuditRow] = []
        self._idem: dict[tuple[str, str], IdemRow] = {}
        self._executed: dict[tuple[str, str], Decimal] = {}
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def lock_order_for_update(self, tenant_id: str, order_id: str) -> Optional[OrderRow]:
        with self._lock:
            return self._orders.get((tenant_id, order_id))

    def get_order(self, tenant_id: str, order_id: str) -> Optional[OrderRow]:
        with self._lock:
            return self._orders.get((tenant_id, order_id))

    def insert_order(self, row: OrderRow) -> None:
        with self._lock:
            self._orders[(row.tenant_id, row.order_id)] = row

    def update_order_versioned(self, row: OrderRow, expected_version: int) -> None:
        with self._lock:
            cur = self._orders.get((row.tenant_id, row.order_id))
            if cur is None or cur.version != expected_version:
                raise OptimisticLockError("订单版本不匹配")
            self._orders[(row.tenant_id, row.order_id)] = replace(row, version=expected_version + 1)

    def insert_ticket(self, row: TicketRow) -> None:
        with self._lock:
            self._tickets[(row.tenant_id, row.ticket_id)] = row

    def get_ticket(self, tenant_id: str, ticket_id: str) -> Optional[TicketRow]:
        with self._lock:
            return self._tickets.get((tenant_id, ticket_id))

    def insert_operation(self, row: OperationRow) -> None:
        with self._lock:
            self._operations[(row.tenant_id, row.operation_id)] = row

    def get_operation(self, tenant_id: str, operation_id: str) -> Optional[OperationRow]:
        with self._lock:
            return self._operations.get((tenant_id, operation_id))

    def update_operation_versioned(self, row: OperationRow, expected_version: int) -> None:
        with self._lock:
            cur = self._operations.get((row.tenant_id, row.operation_id))
            if cur is None or cur.version != expected_version:
                raise OptimisticLockError("操作版本不匹配")
            self._operations[(row.tenant_id, row.operation_id)] = replace(row, version=expected_version + 1)

    def executed_sum_for_order(self, tenant_id: str, order_id: str) -> Decimal:
        with self._lock:
            return self._executed.get((tenant_id, order_id), Decimal("0.00"))

    def try_execute_refund(self, tenant_id: str, order_id: str, operation: OperationRow) -> bool:
        with self._lock:
            order = self._orders.get((tenant_id, order_id))
            if order is None:
                return False
            current = self._executed.get((tenant_id, order_id), Decimal("0.00"))
            if current + (operation.amount or Decimal("0.00")) > order.paid_amount:
                return False
            self._operations[(tenant_id, operation.operation_id)] = operation
            self._executed[(tenant_id, order_id)] = current + (operation.amount or Decimal("0.00"))
            return True

    def insert_approval(self, row: ApprovalRow) -> None:
        with self._lock:
            self._approvals.append(row)

    def approvals_of(self, tenant_id: str, operation_id: str) -> list[ApprovalRow]:
        with self._lock:
            return [a for a in self._approvals
                    if a.tenant_id == tenant_id and a.operation_id == operation_id]

    def insert_audit(self, row: AuditRow) -> None:
        with self._lock:
            self._audits.append(row)

    def audit_of(self, tenant_id: str, entity_type: str, entity_id: str) -> list[AuditRow]:
        with self._lock:
            return [a for a in self._audits
                    if a.tenant_id == tenant_id and a.entity_type == entity_type
                    and a.entity_id == entity_id]

    def insert_idem(self, row: IdemRow) -> None:
        with self._lock:
            key = (row.tenant_id, row.idem_key)
            if key in self._idem:
                raise UniqueViolation(f"幂等键 {row.idem_key} 已存在（租户 {row.tenant_id}）")
            self._idem[key] = row

    def get_idem(self, tenant_id: str, idem_key: str) -> Optional[IdemRow]:
        with self._lock:
            return self._idem.get((tenant_id, idem_key))
