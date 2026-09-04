"""AfterSales 持久化端口（K4）：唯一事实源 Repository 契约。

- 所有接口以 tenant_id 显式作用域；实现必须保证租户隔离；
- 订单行级锁（FOR UPDATE 语义）用于订单级退款额度并发保护；
- 乐观版本（update … WHERE version=expected）覆盖写路径；
- 幂等记录唯一约束（同 (tenant, key) 第二次插入 → UniqueViolation）。

领域状态机仍由 src.domain.after_sales 裁决；本层仅负责“把已裁决事实持久化”与
“跨进程并发下的唯一/额度保护”，不作为业务真相来源之外的决策者。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator, Optional


class UniqueViolation(Exception):
    """唯一约束冲突（幂等键已存在）。"""


class OptimisticLockError(Exception):
    """版本不匹配（乐观并发冲突）。"""


@dataclass(frozen=True)
class OrderRow:
    tenant_id: str
    order_id: str
    customer_id: str
    status: str
    paid_amount: Decimal
    days_since_sign: int
    version: int = 1


@dataclass(frozen=True)
class TicketRow:
    tenant_id: str
    ticket_id: str
    order_id: str
    customer_id: str
    request_type: str
    reason: str
    status: str
    resolution: Optional[str] = None
    version: int = 1


@dataclass(frozen=True)
class OperationRow:
    tenant_id: str
    operation_id: str
    ticket_id: str
    order_id: str
    op_type: str
    amount: Optional[Decimal]
    status: str
    idempotency_key: Optional[str]
    created_by: str
    version: int = 1
    decision_version: Optional[int] = None
    executed: bool = False


@dataclass(frozen=True)
class ApprovalRow:
    tenant_id: str
    operation_id: str
    decision: str            # approved | rejected
    reason: Optional[str]
    decided_by: str
    decided_version: int


@dataclass(frozen=True)
class AuditRow:
    tenant_id: str
    action: str
    entity_type: str
    entity_id: str
    actor: str
    before_state: Optional[str]
    after_state: Optional[str]
    idempotency_key: Optional[str] = None
    note: Optional[str] = None


@dataclass(frozen=True)
class IdemRow:
    tenant_id: str
    idem_key: str
    payload_hash: str
    refund_id: str


class AfterSalesRepository(ABC):
    """表级 Repository 契约（实现：内存 / PostgreSQL）。"""

    # ---------- 事务与行锁 ----------
    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """事务边界（内存实现为 no-op；PG 实现 BEGIN/COMMIT/ROLLBACK）。"""

    @abstractmethod
    def lock_order_for_update(self, tenant_id: str, order_id: str) -> Optional[OrderRow]:
        """以行锁（PG：SELECT … FOR UPDATE）读取订单，用于订单级额度保护。"""

    # ---------- orders ----------
    @abstractmethod
    def get_order(self, tenant_id: str, order_id: str) -> Optional[OrderRow]: ...

    @abstractmethod
    def insert_order(self, row: OrderRow) -> None: ...

    @abstractmethod
    def update_order_versioned(self, row: OrderRow, expected_version: int) -> None:
        """乐观更新；版本不符抛 OptimisticLockError。"""

    # ---------- tickets ----------
    @abstractmethod
    def insert_ticket(self, row: TicketRow) -> None: ...

    @abstractmethod
    def get_ticket(self, tenant_id: str, ticket_id: str) -> Optional[TicketRow]: ...

    # ---------- refund_operations ----------
    @abstractmethod
    def insert_operation(self, row: OperationRow) -> None: ...

    @abstractmethod
    def get_operation(self, tenant_id: str, operation_id: str) -> Optional[OperationRow]: ...

    @abstractmethod
    def update_operation_versioned(self, row: OperationRow, expected_version: int) -> None: ...

    @abstractmethod
    def executed_sum_for_order(self, tenant_id: str, order_id: str) -> Decimal:
        """该订单 status='executed' 的退款累计（容量校验用）。"""

    @abstractmethod
    def try_execute_refund(self, tenant_id: str, order_id: str, operation: OperationRow) -> bool:
        """行锁内原子容量执行：累计+amount ≤ 实付才插入 executed 操作并返回 True；
        否则返回 False（不插入、不累计）。并发安全由实现保证（PG: SELECT FOR UPDATE）。"""

    # ---------- approval_decisions ----------
    @abstractmethod
    def insert_approval(self, row: ApprovalRow) -> None: ...

    @abstractmethod
    def approvals_of(self, tenant_id: str, operation_id: str) -> list[ApprovalRow]: ...

    # ---------- audit_events ----------
    @abstractmethod
    def insert_audit(self, row: AuditRow) -> None: ...

    @abstractmethod
    def audit_of(self, tenant_id: str, entity_type: str, entity_id: str) -> list[AuditRow]: ...

    # ---------- idempotency_records（唯一约束） ----------
    @abstractmethod
    def insert_idem(self, row: IdemRow) -> None:
        """同 (tenant_id, idem_key) 已存在 → UniqueViolation。"""

    @abstractmethod
    def get_idem(self, tenant_id: str, idem_key: str) -> Optional[IdemRow]: ...
