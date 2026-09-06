"""持久化编解码层（生产路径）。

- `row_codecs`：PG 行 ↔ 领域对象编解码，供 PgCommandService / PgCommandAdapter 使用
  （读 Repository 行 → 领域对象 / 领域对象 → 行）。

历史原型（已删除，不留死引用）：
- SQLite append-only journal + 快照恢复原型（RecoverableSession/SQLiteSnapshotStore/
  validate_snapshot_state/state_from_jsonable）——不属于默认 API / WorkflowRunner / PG
  profile / 黄金集路径，已随 V1 收口删除；
- PgBackedSession 整库镜像会话（save/load + clear_all 重插）——非生产命令路径，已删除；
  SQLite 仍仅作 LangGraph checkpoint（langgraph-checkpoint-sqlite）使用，不存业务真相。
"""
from .row_codecs import (
    audit_from_row,
    operation_from_row,
    operation_to_row,
    order_from_row,
    order_item_to_row,
    order_to_row,
    policy_from_row,
    policy_to_row,
    ticket_from_row,
    ticket_to_row,
)

__all__ = [
    "audit_from_row",
    "operation_from_row",
    "operation_to_row",
    "order_from_row",
    "order_item_to_row",
    "order_to_row",
    "policy_from_row",
    "policy_to_row",
    "ticket_from_row",
    "ticket_to_row",
]
