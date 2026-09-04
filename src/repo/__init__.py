"""K4 唯一事实源 Repository 层（接口 + 内存 + PostgreSQL 实现）。

领域状态机仍由 src.domain.after_sales 裁决；本层提供“持久化端口”与
跨进程并发下的唯一约束/行锁/乐观版本语义。SQLite 仅为恢复原型，不是生产事实源。
"""
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
from .memory import MemoryAfterSalesRepository
from .postgres import PostgresAfterSalesRepository

__all__ = [
    "AfterSalesRepository",
    "ApprovalRow",
    "AuditRow",
    "IdemRow",
    "MemoryAfterSalesRepository",
    "OperationRow",
    "OptimisticLockError",
    "OrderRow",
    "PostgresAfterSalesRepository",
    "TicketRow",
    "UniqueViolation",
]
