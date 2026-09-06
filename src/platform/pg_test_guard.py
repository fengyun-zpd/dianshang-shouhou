"""PostgreSQL 破坏性操作测试库守卫（V1 验收，fail-closed）。

约束（AGENTS.md 第五条/第九条）：任何 DROP / 重建 schema / 迁移回放**只允许**显式
隔离测试库。本模块把该约束固化为可执行守卫：

- `is_allowed_test_db_url(url)`：解析 SQLAlchemy URL，仅当 host ∈ {localhost,
  127.0.0.1} 且 database 名以 ``opspilot_test_`` 开头（且不是共享/系统库名
  opspilot/postgres/template0/template1）才放行；
- `require_isolated_test_db(url)`：不满足 → 抛 `TestDbGuardError`（fail-closed，
  错误消息给出被拒原因分类：非 localhost / 非测试库名 / 共享或系统库 / 缺库名或畸形）；
- `reset_test_schema(url)`：guard 通过后 DROP 全部业务表 + 执行 src/repo/schema.sql
  重建（业务表清单从 schema.sql 动态提取，新表自动纳入；alembic_version 不受影响）；
  guard 拒绝时**不执行任何 SQL**，直接抛错；
- `test_database_url_from_env()`：破坏性操作目标库**只允许**来自
  ``OPSPILOT_TEST_DATABASE_URL``，**不回退** ``DATABASE_URL``（避免误把共享 opspilot
  主库作为 DROP 目标）。

业务 live 测试（tests/ 下 9 个破坏性集成文件）统一经 tests/pg_live.py 入口调用本
守卫；evals/replay.py 的 PgReplayProfile.reset_schema 同样受本守卫保护。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

#: 隔离测试库命名约束：仅允许 opspilot_test_* 前缀
TEST_DB_PREFIX = "opspilot_test_"
#: 允许承载破坏性重置的主机（本机回环）
ALLOWED_TEST_DB_HOSTS = {"localhost", "127.0.0.1"}
#: 共享主库 / PostgreSQL 系统库名 —— 永远不允许作为 DROP/重建目标
FORBIDDEN_DB_NAMES = {"opspilot", "postgres", "template0", "template1", "template01"}

_SCHEMA_SQL = (Path(__file__).resolve().parents[2] / "src" / "repo" / "schema.sql")
_CREATE_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-zA-Z_][a-zA-Z0-9_]*)")


class TestDbGuardError(RuntimeError):
    """破坏性 PostgreSQL 操作被守卫拒绝（fail-closed，未执行任何 SQL）。"""

    __test__ = False  # 非 pytest 测试类（名字 Test* 前缀会被收集器误扫）


def reject_reason(url: str) -> Optional[str]:
    """返回拒绝原因分类文本；URL 允许执行破坏性重置时返回 None。

    判定顺序：① 非本机 host → "非 localhost"；② 缺库名/畸形 URL → 缺库名或畸形；
    ③ 共享/系统库名 → 共享或系统库；④ 非 ``opspilot_test_*`` 前缀 → 非测试库名。
    """
    try:
        u = make_url(url)
    except (ArgumentError, ValueError, TypeError):
        return f"拒绝：无法解析数据库 URL（畸形或缺失），破坏性重置 fail-closed"
    host = (u.host or "").lower()
    database = u.database
    if host not in ALLOWED_TEST_DB_HOSTS:
        shown = host if host else "<无 host>"
        return (f"拒绝：目标主机 {shown!r} 非本机"
                f"（破坏性重置仅允许 localhost/127.0.0.1 的隔离测试库）")
    if not database:
        return "拒绝：URL 缺少数据库名（无法确认隔离测试库），破坏性重置 fail-closed"
    if database in FORBIDDEN_DB_NAMES:
        return (f"拒绝：目标库 {database!r} 是共享主库或 PostgreSQL 系统库，"
                f"禁止作为 DROP/重建目标")
    if not database.startswith(TEST_DB_PREFIX):
        return (f"拒绝：目标库 {database!r} 非 {TEST_DB_PREFIX}* 隔离测试库"
                f"（破坏性重置只允许显式隔离测试库）")
    return None


def is_allowed_test_db_url(url: str) -> bool:
    """URL 是否允许承载破坏性重置（host 本机 + database 为 opspilot_test_*）。"""
    return reject_reason(url) is None


def require_isolated_test_db(url: str) -> str:
    """守卫检查：拒绝 → 抛 TestDbGuardError；通过 → 返回原 url（fail-closed）。"""
    reason = reject_reason(url)
    if reason is not None:
        raise TestDbGuardError(
            f"{reason}。破坏性集成请设置 OPSPILOT_TEST_DATABASE_URL 指向隔离测试库"
            f"（如 postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/{TEST_DB_PREFIX}...）")
    return url


def test_database_url_from_env() -> Optional[str]:
    """破坏性测试目标库唯一来源：OPSPILOT_TEST_DATABASE_URL。

    刻意**不回退** DATABASE_URL：DATABASE_URL 可能指向共享 opspilot 主库，
    不允许作为隐式 DROP 目标。
    """
    return os.environ.get("OPSPILOT_TEST_DATABASE_URL") or None


def business_table_names(schema_sql: Optional[str] = None) -> list[str]:
    """从 schema.sql 文本提取业务表名（按出现顺序去重，alembic_version 不在其中）。"""
    if schema_sql is None:
        schema_sql = schema_sql_text()
    seen: list[str] = []
    for m in _CREATE_TABLE_RE.finditer(schema_sql):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def schema_sql_text() -> str:
    """读取 src/repo/schema.sql（锚定仓库根，不依赖 cwd）。"""
    return _SCHEMA_SQL.read_text(encoding="utf-8")


def reset_test_schema(url: str, schema_sql: Optional[str] = None) -> None:
    """守卫通过后重建全部业务表：DROP（CASCADE）+ 执行 schema.sql。

    - guard 拒绝（require_isolated_test_db 抛错）→ **不创建连接、不执行任何 SQL**；
    - 业务表清单从 schema.sql 动态提取（新表自动纳入，无需维护第二份拷贝）；
    - alembic_version 表不受影响（schema.sql 不建它，DROP 只针对业务表）。
    """
    require_isolated_test_db(url)          # fail-closed：拒绝即抛，无 SQL 副作用
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    if schema_sql is None:
        schema_sql = schema_sql_text()
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for table in business_table_names(schema_sql):
                conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            conn.execute(text(schema_sql))
    finally:
        engine.dispose()
