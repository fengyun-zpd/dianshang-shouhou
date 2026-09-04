"""阶段 5 之后工程（生产化原型）：SQLite 可恢复唯一事实源。"""
from .codec import state_from_jsonable, state_to_jsonable
from .session import RecoverableSession
from .store import SQLiteSnapshotStore, SnapshotCorruptionError

__all__ = [
    "RecoverableSession",
    "SQLiteSnapshotStore",
    "SnapshotCorruptionError",
    "state_from_jsonable",
    "state_to_jsonable",
]
