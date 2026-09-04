"""可恢复唯一事实源原型（任务卡 J：严格校验 + 原子恢复）。"""
from .codec import state_from_jsonable, state_to_jsonable
from .session import RecoverableSession
from .store import SQLiteSnapshotStore, SnapshotCorruptionError
from .validate import validate_snapshot_state

__all__ = [
    "RecoverableSession",
    "SQLiteSnapshotStore",
    "SnapshotCorruptionError",
    "state_from_jsonable",
    "state_to_jsonable",
    "validate_snapshot_state",
]
