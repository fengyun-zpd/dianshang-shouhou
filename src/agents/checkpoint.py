"""可持久化 LangGraph checkpoint（阶段三：持久化工作流）。

宪法边界（AGENTS.md 第 8/9 条与本模块 docstring）：
- checkpoint 只保存流程恢复状态（节点位置 / 请求指纹 / 租户 / 最小流程字段）；
  金额、业务状态、审批决定与执行结果等业务事实一律重读领域服务（内存或 PG-backed），
  禁止从 checkpoint 恢复或覆盖业务真相；
- SqliteSaver 把 checkpoint 持久化到 SQLite 文件；进程重启后以同一文件重开，
  可从原 thread_id 继续 resume（不重放、无新副作用）——业务流程可跨进程恢复，
  业务事实则以 PG 行表（PgBackedSession）或内存领域服务为准。
"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver


def open_sqlite_checkpointer(db_path: str) -> SqliteSaver:
    """打开（必要时创建）SQLite 持久 checkpointer；多进程/重启共享同一文件即可续跑。

    返回可直接传给 WorkflowRunner(checkpointer=...) 的 SqliteSaver（连接已建表并保持打开，
    调用方需在其生命周期内保持引用；进程退出前可 close()）。
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    saver = SqliteSaver(conn)
    saver.setup()
    return saver
