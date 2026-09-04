"""可恢复会话：从 SQLite 快照重建领域服务并可继续工作流（可恢复持久化原型）。

用法：
    session = RecoverableSession(db_path=..., build_service=make_service)
    svc = session.load()          # 有快照 → 恢复；否则全新
    ... 执行领域/工作流命令 ...
    session.persist(svc)          # 落库（append-only 快照）

恢复保真：订单/政策/工单/操作/累计退款/幂等记录/审计/序号 全部恢复，
恢复后可继续审批/执行（状态机与幂等语义不变）。
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Optional

from src.domain.after_sales import AfterSalesService

from .codec import SnapshotCodecError, state_from_jsonable, state_to_jsonable
from .store import SQLiteSnapshotStore, SnapshotCorruptionError

__all__ = ["RecoverableSession", "SnapshotCodecError", "SnapshotCorruptionError",
           "SQLiteSnapshotStore"]


class RecoverableSession:
    def __init__(self, db_path, build_service: Optional[Callable[[], AfterSalesService]] = None):
        self._store = SQLiteSnapshotStore(db_path)
        self._build_service = build_service or (lambda: AfterSalesService())

    def load(self) -> AfterSalesService:
        """加载：有快照 → 校验并恢复；否则返回全新服务。"""
        snapshot = self._store.latest_snapshot()
        svc = self._build_service()
        if snapshot is not None:
            try:
                state = state_from_jsonable(snapshot)
            except (KeyError, TypeError, ValueError) as e:
                raise SnapshotCorruptionError(f"快照解码失败：{e}") from e
            svc.restore_state(state)
        return svc

    def persist(self, svc: AfterSalesService) -> int:
        """把当前领域状态编码为可 JSON 快照并 append 落库。返回记录 id。"""
        jsonable = state_to_jsonable(svc.export_state())
        return self._store.save_snapshot(jsonable)

    @property
    def store(self) -> SQLiteSnapshotStore:
        return self._store

    def close(self) -> None:
        self._store.close()
