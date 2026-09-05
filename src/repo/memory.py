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
    OrderItemRow,
    OrderRow,
    PolicyRow,
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
        self._policies: list[PolicyRow] = []
        self._order_items: dict[tuple[str, str, str], OrderItemRow] = {}
        self._seq: dict[tuple[str, str], int] = {}
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    @contextmanager
    def unit_of_work(self) -> Iterator[None]:
        """内存原子写模拟：作用域内持全局锁 + 快照全部容器；异常 → 恢复快照（无部分提交）；
        不允许嵌套。作用域内读写方法各持 RLock（可重入），串行化到同一把锁上。"""
        with self._lock:
            if getattr(self, "_uow_active", False):
                raise RuntimeError("unit_of_work 不允许嵌套")
            snap = (dict(self._orders), dict(self._tickets), dict(self._operations),
                    list(self._approvals), list(self._audits), dict(self._idem),
                    dict(self._executed), list(self._policies),
                    dict(self._order_items), dict(self._seq))
            self._uow_active = True
            try:
                yield
            except BaseException:
                (self._orders, self._tickets, self._operations, self._approvals,
                 self._audits, self._idem, self._executed, self._policies,
                 self._order_items, self._seq) = snap
                raise
            finally:
                self._uow_active = False

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

    # ---------- 恢复装载（全表列举，只读） ----------
    def list_orders(self) -> list[OrderRow]:
        with self._lock:
            return list(self._orders.values())

    def list_tickets(self) -> list[TicketRow]:
        with self._lock:
            return list(self._tickets.values())

    def list_operations(self) -> list[OperationRow]:
        with self._lock:
            return list(self._operations.values())

    def list_audit(self) -> list[AuditRow]:
        with self._lock:
            return list(self._audits)

    def list_idem(self) -> list[IdemRow]:
        with self._lock:
            return list(self._idem.values())

    def clear_all(self) -> None:
        with self._lock:
            self._orders.clear()
            self._tickets.clear()
            self._operations.clear()
            self._approvals.clear()
            self._audits.clear()
            self._idem.clear()
            self._executed.clear()
            self._policies.clear()
            self._order_items.clear()
            self._seq.clear()

    # ---------- 0004 命令数据面 ----------
    def insert_policy(self, row: PolicyRow) -> None:
        with self._lock:
            key = (row.tenant_id, row.policy_id, row.version)
            if any((p.tenant_id, p.policy_id, p.version) == key for p in self._policies):
                raise UniqueViolation(f"政策 {row.policy_id} v{row.version} 已存在（租户 {row.tenant_id}）")
            self._policies.append(row)

    def list_policies(self) -> list[PolicyRow]:
        with self._lock:
            return list(self._policies)

    def insert_order_item(self, row: OrderItemRow) -> None:
        with self._lock:
            key = (row.tenant_id, row.order_id, row.sku)
            if key in self._order_items:
                raise UniqueViolation(f"明细 {row.sku} 已存在（{row.order_id}）")
            self._order_items[key] = row

    def list_order_items(self) -> list[OrderItemRow]:
        with self._lock:
            return list(self._order_items.values())

    def next_seq(self, tenant_id: str, kind: str) -> int:
        with self._lock:
            key = (tenant_id, kind)
            self._seq[key] = self._seq.get(key, 0) + 1
            return self._seq[key]
