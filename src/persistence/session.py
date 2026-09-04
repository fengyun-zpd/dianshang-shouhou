"""可恢复会话：从 SQLite 快照重建领域服务并可继续工作流（可恢复持久化原型）。

恢复管线（任务卡 J）：读取 → checksum 校验 → JSON 解码 → 严格结构/语义校验
（validate_snapshot_state）→ 原子替换（restore_state 构造完成后一次性赋值）。

- 任何解码/校验/恢复错误统一转 SnapshotCorruptionError；
- 校验失败时调用方提供的原服务状态完全不变（先验后换，原子恢复）。
"""
from __future__ import annotations

from decimal import InvalidOperation
from typing import Callable, Optional

from src.domain.after_sales import AfterSalesService

from .codec import state_from_jsonable, state_to_jsonable
from .store import SQLiteSnapshotStore, SnapshotCorruptionError
from .validate import validate_snapshot_state

__all__ = ["RecoverableSession", "SnapshotCorruptionError", "SQLiteSnapshotStore",
           "validate_snapshot_state"]


class RecoverableSession:
    def __init__(self, db_path, build_service: Optional[Callable[[], AfterSalesService]] = None):
        self._store = SQLiteSnapshotStore(db_path)
        self._build_service = build_service or (lambda: AfterSalesService())

    def load(self) -> AfterSalesService:
        """加载：有快照则解码→校验→恢复；否则返回全新服务。"""
        snapshot = self._store.latest_snapshot()
        svc = self._build_service()
        if snapshot is not None:
            state = self._decode_and_validate(snapshot)
            svc.restore_state(state)
        return svc

    def restore_into(self, svc: AfterSalesService, jsonable_snapshot: dict) -> None:
        """把快照解码并严格校验后原子恢复到传入服务；失败时 svc 完全不变。"""
        state = self._decode_and_validate(jsonable_snapshot)
        svc.restore_state(state)

    def persist(self, svc: AfterSalesService) -> int:
        """把当前领域状态编码为可 JSON 快照并 append 落库。返回记录 id。"""
        jsonable = state_to_jsonable(svc.export_state())
        return self._store.save_snapshot(jsonable)

    def _decode_and_validate(self, snapshot: dict) -> dict:
        try:
            state = state_from_jsonable(snapshot)
        except (KeyError, TypeError, ValueError, InvalidOperation) as e:
            raise SnapshotCorruptionError(f"快照解码失败：{e}") from e
        validate_snapshot_state(state)  # 违规 → SnapshotCorruptionError
        return state

    @property
    def store(self) -> SQLiteSnapshotStore:
        return self._store

    def close(self) -> None:
        self._store.close()
