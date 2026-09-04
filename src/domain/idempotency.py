"""幂等键存储：拦截重复请求，保证同载荷不重复执行。"""
from __future__ import annotations

import hashlib
import json
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


class IdempotencyStore:
    """内存幂等键存储；生产应替换为 PostgreSQL 唯一约束 + 行级锁。

    语义（对齐宪法第四条）：
    - 同键同载荷：返回首次执行结果，不重复执行；
    - 同键异载荷：拒绝；
    - 外部结果未知：只查询原键，禁止换键重试。
    """

    def __init__(self):
        self._records: dict[str, IdempotencyRecord] = {}

    def get(self, key: str) -> Optional[IdempotencyRecord]:
        return self._records.get(key)

    def register(self, key: str, payload_hash_: str, refund_id: str) -> None:
        self._records[key] = IdempotencyRecord(payload_hash=payload_hash_, refund_id=refund_id)
