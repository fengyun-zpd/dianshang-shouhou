"""PG destructive-operation guard 单元测试（V1 验收，不依赖真实 PostgreSQL）。

覆盖（真实断言，非字符串摆设）：
- 合法隔离测试库 URL（opspilot_test_*@localhost/127.0.0.1）→ 允许；
- 非本机 host（db.example.com / 生产域名）→ 拒绝（分类：非 localhost）；
- 共享/系统库名（opspilot/postgres/template0/template1）→ 拒绝；
- 非 opspilot_test_* 前缀库名 / 缺库名 / 畸形 URL → 拒绝；
- reset_test_schema：拒绝 URL → 抛 TestDbGuardError 且不创建连接/不执行 SQL；
  合法 URL → guard 通过后真实执行 DROP 全表 + schema.sql（mock 引擎收集调用）；
- business_table_names 从 schema.sql 提取 10 张业务表（新表自动纳入）；
- 测试库 URL 只来自 OPSPILOT_TEST_DATABASE_URL，不回退 DATABASE_URL。
"""
from __future__ import annotations

import pytest

from src.platform.pg_test_guard import (
    TestDbGuardError,
    business_table_names,
    is_allowed_test_db_url,
    reject_reason,
    require_isolated_test_db,
    reset_test_schema,
    schema_sql_text,
    test_database_url_from_env,
)
from tests.pg_live import pg_reachable

# ---------- 合法隔离测试库 ----------

ALLOWED_URLS = [
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot_test_v1",
    "postgresql+psycopg2://opspilot:opspilot@localhost:5433/opspilot_test_a",
    "postgresql://opspilot:opspilot@127.0.0.1:5433/opspilot_test_isolated_2",
]


@pytest.mark.parametrize("url", ALLOWED_URLS)
def test_allowed_isolated_test_db_urls(url: str):
    assert is_allowed_test_db_url(url) is True
    assert reject_reason(url) is None
    assert require_isolated_test_db(url) == url


# ---------- 拒绝路径：非本机 host ----------

REJECT_REMOTE_HOST_URLS = [
    "postgresql+psycopg2://opspilot:opspilot@db.example.com:5432/opspilot_test_v1",
    "postgresql://opspilot:pw@prod-db.internal:5432/opspilot_test_v1",
    "postgresql://opspilot:pw@10.0.0.8:5432/opspilot_test_v1",
]


@pytest.mark.parametrize("url", REJECT_REMOTE_HOST_URLS)
def test_reject_remote_host(url: str):
    """非 localhost/127.0.0.1 host → 拒绝（fail-closed，分类：非本机）。"""
    assert is_allowed_test_db_url(url) is False
    assert "localhost" in reject_reason(url)


# ---------- 拒绝路径：共享/系统库名 ----------

REJECT_SHARED_DB_URLS = [
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot",
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/postgres",
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/template0",
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/template1",
    "postgresql+psycopg2://opspilot:opspilot@localhost:5433/opspilot",
]


@pytest.mark.parametrize("url", REJECT_SHARED_DB_URLS)
def test_reject_shared_or_system_db(url: str):
    """共享主库/系统库名（含 DATABASE_URL 默认回退的 opspilot）→ 拒绝。"""
    assert is_allowed_test_db_url(url) is False
    reason = reject_reason(url)
    assert "共享主库或 PostgreSQL 系统库" in reason


# ---------- 拒绝路径：非测试库名 / 缺库名 / 畸形 ----------

REJECT_OTHER_URLS = [
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot_dev",   # 非前缀
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot_testing",  # 缺下划线
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/mydb",
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/",              # 缺库名
    "not-a-url",                                                              # 畸形
    "",                                                                       # 空
]


@pytest.mark.parametrize("url", REJECT_OTHER_URLS)
def test_reject_non_test_db_malformed(url: str):
    """非 opspilot_test_* 前缀 / 缺库名 / 畸形 URL → 拒绝（不静默放行）。"""
    assert is_allowed_test_db_url(url) is False
    assert reject_reason(url) is not None


def test_reject_reason_categories_are_distinguishable():
    """拒绝原因分类可区分（非本机 / 共享系统库 / 非前缀 / 缺库名畸形）。"""
    assert "localhost" in reject_reason(
        "postgresql://u:p@db.example.com/x/opspilot_test_v1")
    assert "共享主库或 PostgreSQL 系统库" in reject_reason(
        "postgresql://u:p@127.0.0.1/opspilot")
    assert "非 opspilot_test_*" in reject_reason(
        "postgresql://u:p@127.0.0.1/opspilot_main")
    assert "缺少数据库名" in reject_reason("postgresql://u:p@127.0.0.1/")
    assert "无法解析" in reject_reason("nonsense")


# ---------- reset_test_schema：拒绝 → 无任何 SQL ----------

class _NoEngine:
    """若被创建即为测试失败信号：guard 拒绝路径不允许创建连接。"""

    def __init__(self, *a, **k):
        raise AssertionError("guard 拒绝时不得创建数据库连接")


def test_reset_rejected_url_raises_without_sql(monkeypatch):
    """reset_test_schema 对拒绝 URL 抛 TestDbGuardError，且不执行任何 SQL。"""
    monkeypatch.setattr("sqlalchemy.create_engine", _NoEngine)
    with pytest.raises(TestDbGuardError):
        reset_test_schema(
            "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot")
    with pytest.raises(TestDbGuardError):
        reset_test_schema(
            "postgresql+psycopg2://opspilot:opspilot@db.example.com:5432/opspilot_test_v1")
    with pytest.raises(TestDbGuardError):
        reset_test_schema("not-a-url")


# ---------- reset_test_schema：合法 URL → guard 通过后真实 DROP+重建 ----------

class _FakeConn:
    def __init__(self):
        self.calls: list[str] = []

    def execute(self, stmt, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls.append(str(stmt))
        return object()


class _FakeTx:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def __enter__(self) -> _FakeConn:
        return self._conn

    def __exit__(self, *exc) -> bool:  # noqa: ANN002
        return False


class _FakeEngine:
    def __init__(self):
        self.conn = _FakeConn()

    def begin(self) -> _FakeTx:
        return _FakeTx(self.conn)

    def dispose(self) -> None:
        pass


def test_reset_allowed_url_drops_all_business_tables_and_applies_schema(monkeypatch):
    """guard 通过 → 真实执行 DROP 全表（业务表清单来自 schema.sql）+ schema.sql。"""
    fake = _FakeEngine()
    monkeypatch.setattr("sqlalchemy.create_engine", lambda url: fake)
    reset_test_schema(
        "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot_test_v1")
    drops = [c for c in fake.conn.calls if str(c).startswith("DROP TABLE")]
    for table in business_table_names():
        assert any(f"DROP TABLE IF EXISTS {table} CASCADE" in c for c in drops), table
    assert len(fake.conn.calls) == len(business_table_names()) + 1   # 每表一次 DROP + schema.sql
    assert fake.conn.calls[-1].lstrip().startswith("--") or "CREATE TABLE" in fake.conn.calls[-1]


def test_business_table_names_extracted_from_schema_sql():
    """表清单从 schema.sql 动态提取：10 张业务表全量纳入（含 workflow_threads/entity_seq）。"""
    names = business_table_names()
    assert names == [
        "orders", "tickets", "refund_operations", "approval_decisions",
        "idempotency_records", "audit_events", "policies", "order_items",
        "entity_seq", "workflow_threads",
    ]
    assert "alembic_version" not in schema_sql_text() or "alembic_version" not in names


# ---------- 环境变量来源：不回退 DATABASE_URL ----------

def test_test_db_url_only_from_ops_environment(monkeypatch):
    """破坏性目标只来自 OPSPILOT_TEST_DATABASE_URL；绝不回退 DATABASE_URL（主库）。"""
    monkeypatch.delenv("OPSPILOT_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot")
    assert test_database_url_from_env() is None       # 仅 DATABASE_URL → 无破坏性目标
    monkeypatch.setenv(
        "OPSPILOT_TEST_DATABASE_URL",
        "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot_test_v1")
    assert test_database_url_from_env() is not None
    assert test_database_url_from_env().endswith("opspilot_test_v1")


def test_pg_reachable_rejects_invalid_explicit_url_before_connect(monkeypatch):
    """live 探测对显式非法 URL 失败，不把错误配置静默成 skip。"""
    monkeypatch.setattr("sqlalchemy.create_engine", _NoEngine)
    with pytest.raises(TestDbGuardError):
        pg_reachable("postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot")
