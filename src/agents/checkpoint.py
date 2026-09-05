"""可持久化 LangGraph checkpoint（阶段三/四：持久化工作流）。

宪法边界（AGENTS.md 第 8/9 条与本模块 docstring）：
- checkpoint 只保存流程恢复状态（节点位置 / 请求指纹 / 租户 / 最小流程字段）；
  金额、业务状态、审批决定与执行结果等业务事实一律重读领域服务（内存或 PG-backed），
  禁止从 checkpoint 恢复或覆盖业务真相；
- SqliteSaver 把 checkpoint 持久化到 SQLite 文件（WAL 模式 + busy_timeout）；
  进程重启后以同一文件重开，可从原 thread_id 继续 resume（不重放、无新副作用）；
- 损坏处理：SQLite 文件损坏时 SqliteSaver 读取会抛 sqlite3.DatabaseError → 恢复方 fail-fast
  （不得静默使用损坏的流程状态）；连接生命周期由调用方保持，进程退出自动关闭，
  亦可显式调用 `close_sqlite_checkpointer`。
"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

_WAL_PRAGMA = "PRAGMA journal_mode=WAL"
_BUSY_TIMEOUT_MS = 5000


def open_sqlite_checkpointer(db_path: str) -> SqliteSaver:
    """打开（必要时创建）SQLite 持久 checkpointer；多进程/重启共享同一文件即可续跑。

    加固：WAL 日志（并发读写不互锁）+ busy_timeout=5000ms（多进程写竞争等待而非立即失败）+
    row_factory；连接已建表并保持打开（生命周期由调用方保持，退出自动关闭）。
    返回可直接传给 WorkflowRunner(checkpointer=...) 的 SqliteSaver。
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(_WAL_PRAGMA)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def close_sqlite_checkpointer(saver: SqliteSaver) -> None:
    """显式关闭 checkpointer 底层连接（幂等；进程退出时 SQLite 亦自动关闭）。"""
    conn = None
    if hasattr(saver, "conn"):
        conn = saver.conn
    elif hasattr(saver, "cursor"):
        try:
            conn = saver.cursor().connection
        except Exception:  # noqa: BLE001
            conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
