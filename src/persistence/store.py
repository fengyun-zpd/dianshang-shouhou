"""SQLite append-only 快照存储（可恢复持久化原型）。

- journal 表 append-only：每条记录 id/kind/payload/checksum；
- latest_snapshot() 取最新一条并校验 checksum（损坏 → SnapshotCorruptionError，fail-closed）；
- 所有审计随快照落库（快照即事实源全量），另有审计视图查询。
生产替换：PostgreSQL + alembic 迁移（规划）；本模块为无外部依赖的落库原型。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Optional

_KIND_SNAPSHOT = "snapshot"


class SnapshotCorruptionError(Exception):
    """快照损坏 / checksum 不匹配 / JSON 非法。"""


class SQLiteSnapshotStore:
    def __init__(self, db_path):
        self._db_path = str(db_path)
        if str(db_path) != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS journal ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " kind TEXT NOT NULL,"
            " payload TEXT NOT NULL,"
            " checksum TEXT NOT NULL,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        self._conn.commit()

    def save_snapshot(self, snapshot: dict) -> int:
        """追加一条快照（append-only）。返回记录 id。"""
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        cur = self._conn.execute(
            "INSERT INTO journal (kind, payload, checksum) VALUES (?, ?, ?)",
            (_KIND_SNAPSHOT, payload, checksum),
        )
        self._conn.commit()
        return cur.lastrowid

    def latest_snapshot(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT payload, checksum FROM journal WHERE kind=? ORDER BY id DESC LIMIT 1",
            (_KIND_SNAPSHOT,),
        ).fetchone()
        if row is None:
            return None
        payload, checksum = row
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != checksum:
            raise SnapshotCorruptionError("快照 checksum 不匹配（数据可能损坏或被篡改）")
        try:
            return json.loads(payload)
        except json.JSONDecodeError as e:
            raise SnapshotCorruptionError(f"快照 JSON 非法：{e}") from e

    def journal_size(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0])

    def history(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, kind, created_at, length(payload) AS bytes FROM journal ORDER BY id"
        ).fetchall()
        return [{"id": r[0], "kind": r[1], "created_at": r[2], "bytes": r[3]} for r in rows]

    def clear(self) -> None:
        self._conn.execute("DELETE FROM journal")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
