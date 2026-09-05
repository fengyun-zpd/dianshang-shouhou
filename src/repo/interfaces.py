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
    created_by: str = ""                # 创建者角色 value（Alembic 0003）
    reason_tags: Optional[str] = None   # JSON 数组字符串，如 '["damaged","broken"]'


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
    """幂等记录（0005 三元组语义）：唯一键 (tenant_id, command_type, raw_key)。
    idem_key 为 D2 前缀规范键（展示/审计冗余，可含 command_type 前缀）。
    raw_key=None 的旧式调用兼容为 raw_key=idem_key（等价历史 (tenant,key) 语义）。"""
    tenant_id: str
    idem_key: str
    payload_hash: str
    refund_id: str
    command_type: str = ""
    raw_key: Optional[str] = None


@dataclass(frozen=True)
class PolicyRow:
    """政策行（0004）：reason_tags 为 JSON 数组字符串；同 (tenant, policy_id, version) 唯一。"""
    tenant_id: str
    policy_id: str
    request_type: str
    reason_tags: str          # '["damaged","broken"]'
    window_days: int
    refund_ratio: Decimal
    effective_from: str       # 'YYYY-MM-DD'
    version: int = 1


@dataclass(frozen=True)
class OrderItemRow:
    """订单明细行（0004）：退款上限以 orders.paid_amount 为准，明细供展示/证据完整恢复。"""
    tenant_id: str
    order_id: str
    sku: str
    name: str
    quantity: int
    unit_price: Decimal


class AfterSalesRepository(ABC):
    """表级 Repository 契约（实现：内存 / PostgreSQL）。"""

    # ---------- 事务与行锁 ----------
    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """事务边界（内存实现为 no-op；PG 实现 BEGIN/COMMIT/ROLLBACK）。"""

    @abstractmethod
    def unit_of_work(self) -> AbstractContextManager[None]:
        """命令级原子写作用域：进入后本实例全部读写方法在同一事务/原子语义内执行，
        正常退出提交、异常回滚（无部分提交）；不允许嵌套。
        用途：单个领域命令的多表落库（业务行 + 审批决定 + 审计 + 幂等）必须整体原子。"""

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

    @abstractmethod
    def update_ticket_versioned(self, row: TicketRow, expected_version: int) -> None:
        """工单乐观状态更新（resolve/close 等迁移 CAS）；版本不符抛 OptimisticLockError。"""

    # ---------- refund_operations ----------
    @abstractmethod
    def insert_operation(self, row: OperationRow) -> None: ...

    @abstractmethod
    def get_operation(self, tenant_id: str, operation_id: str) -> Optional[OperationRow]: ...

    @abstractmethod
    def update_operation_versioned(self, row: OperationRow, expected_version: int) -> None:
        """操作乐观更新（状态迁移 CAS）：以 row 的 status/executed/decision_version 为目标值、
        WHERE version=expected 执行并 version+1；版本不符抛 OptimisticLockError。"""

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

    # ---------- 恢复装载（PG-backed 会话 load() 用：全表列举，只读） ----------
    @abstractmethod
    def list_orders(self) -> list[OrderRow]: ...

    @abstractmethod
    def list_tickets(self) -> list[TicketRow]: ...

    @abstractmethod
    def list_operations(self) -> list[OperationRow]: ...

    @abstractmethod
    def list_audit(self) -> list[AuditRow]: ...

    @abstractmethod
    def list_idem(self) -> list[IdemRow]: ...

    @abstractmethod
    def clear_all(self) -> None:
        """清空全部业务行（PG-backed 会话整库镜像写用；实现按 FK 依赖序删除）。"""

    # ---------- 0004 命令数据面：政策/明细装载与租户自增序列 ----------
    @abstractmethod
    def insert_policy(self, row: PolicyRow) -> None:
        """同 (tenant_id, policy_id, version) 已存在 → UniqueViolation。"""

    @abstractmethod
    def list_policies(self) -> list[PolicyRow]:
        """全表政策（恢复装载用）。"""

    @abstractmethod
    def insert_order_item(self, row: OrderItemRow) -> None: ...

    @abstractmethod
    def list_order_items(self) -> list[OrderItemRow]:
        """全表明细（恢复装载用）。"""

    @abstractmethod
    def next_seq(self, tenant_id: str, kind: str) -> int:
        """租户作用域自增序列（kind ∈ ticket|operation）；原子递增并返回新值（PG 单语句）。"""
