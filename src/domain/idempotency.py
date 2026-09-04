"""幂等键存储：拦截重复请求，保证同载荷不重复执行。

原子语义（任务卡 J）：
- lock_for(key)：per-key 锁，供领域服务对同一幂等键串行化 check-then-create（CAS）；
- get_or_reserve(key, phash)：get-or-reserve——无记录则建立 <pending> 占位（返回 None），
  已有记录返回之（同/异载荷由调用方判定）；
- commit / release：创建完成后提交真实指向 / 失败释放占位；
- 外部结果未知：只查询原键，禁止换键重试（语义不变）。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class IdempotencyRecord:
    payload_hash: str
    refund_id: str


def payload_hash(payload: dict) -> str:
    """可复现的载荷哈希（跨进程稳定，不使用 Python 内置 hash）。"""
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_PENDING = "<pending>"

# 占位标记（供领域服务区分“进行中占位”与“已提交记录”）
PENDING_REFUND_ID = _PENDING


class IdempotencyStore:
    """线程安全的内存幂等键存储（含 per-key 原子临界与占位）。

    语义（对齐宪法第四条与任务卡 J）：
    - 同键同载荷：返回首次执行结果，不重复执行；
    - 同键异载荷：拒绝；
    - 外部结果未知：只查询原键，禁止换键重试。
    生产应替换为 PostgreSQL 唯一约束 + 行级锁（规划）。
    """

    def __init__(self):
        self._records: dict[str, IdempotencyRecord] = {}
        self._guard = threading.RLock()
        self._key_locks: dict[str, threading.Lock] = {}

    def lock_for(self, key: str) -> threading.Lock:
        """返回该幂等键的专用锁（同键串行、不同键并行）。"""
        with self._guard:
            return self._key_locks.setdefault(key, threading.Lock())

    def get(self, key: str) -> Optional[IdempotencyRecord]:
        return self._records.get(key)

    def register(self, key: str, payload_hash_: str, refund_id: str) -> None:
        self._records[key] = IdempotencyRecord(payload_hash=payload_hash_, refund_id=refund_id)

    def get_or_reserve(self, key: str, payload_hash_: str) -> Optional[IdempotencyRecord]:
        """get-or-reserve：无记录 → 建立 <pending> 占位并返回 None；
        已有记录（含他人占位）→ 返回该记录。调用方据此判定同载荷复用 / 异载荷冲突。"""
        with self._guard:
            rec = self._records.get(key)
            if rec is not None:
                return rec
            self._records[key] = IdempotencyRecord(payload_hash=payload_hash_, refund_id=_PENDING)
            return None

    def commit(self, key: str, payload_hash_: str, refund_id: str) -> None:
        """创建完成后把占位/记录提交为真实指向。"""
        with self._guard:
            self._records[key] = IdempotencyRecord(payload_hash=payload_hash_, refund_id=refund_id)

    def release(self, key: str) -> None:
        """创建失败时释放占位（仅 <pending> 会被移除）。"""
        with self._guard:
            rec = self._records.get(key)
            if rec is not None and rec.refund_id == _PENDING:
                del self._records[key]

    # ---------- 可恢复持久化（原型）支持 ----------

    def export_records(self) -> dict[str, IdempotencyRecord]:
        """导出全部幂等记录（供快照持久化；语义不受影响）。"""
        return dict(self._records)

    def import_records(self, records: dict[str, IdempotencyRecord]) -> None:
        """从导出记录恢复（仅用于恢复路径；不改变查/注册语义）。"""
        self._records = dict(records)
