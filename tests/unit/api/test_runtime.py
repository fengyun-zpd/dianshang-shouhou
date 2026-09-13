"""Runtime profile 装配与门禁测试（阶段四第 3 节）。

pg profile 必须要求 PostgreSQL 可达 + schema 版本达标；不满足 → RuntimeError，
**绝不静默降级到内存**。
"""
import os

import pytest

from src.api.runtime import require_postgres_ready, RuntimeProfile

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot",
)


def _pg_available() -> bool:
    from src.api.runtime import probe_postgres
    return probe_postgres(DATABASE_URL)


def _pg_schema_version() -> str | None:
    if not _pg_available():
        return None
    from sqlalchemy import create_engine, text
    try:
        engine = create_engine(DATABASE_URL)
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        engine.dispose()
        return version
    except Exception:  # noqa: BLE001 - unavailable shared DB should be skipped
        return None


pg_live = pytest.mark.skipif(_pg_schema_version() != "0006",
                             reason="PostgreSQL 不可达或 schema≠0006：数据库集成未实测")


def test_runtime_profile_values():
    assert RuntimeProfile.MEMORY.value == "memory"
    assert RuntimeProfile.PG.value == "pg"


@pg_live
def test_require_postgres_ready_ok_when_reachable_and_version_ok():
    """可达且 schema=0006 → 返回 url（pg profile 可装配）。"""
    assert require_postgres_ready(DATABASE_URL) == DATABASE_URL


def test_require_postgres_ready_raises_when_unreachable():
    """不可达 → RuntimeError（拒绝静默降级），错误信息明示。"""
    with pytest.raises(RuntimeError, match="拒绝静默降级"):
        require_postgres_ready("postgresql+psycopg2://u:p@127.0.0.1:59999/nope",
                               required_version="0005")


def test_require_postgres_ready_raises_on_version_mismatch(monkeypatch):
    """版本不符 → RuntimeError（不 fallback）。"""
    monkeypatch.setattr("src.api.runtime.probe_postgres", lambda url: True)

    class _Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, stmt):
            return _Fake()

        def scalar(self):
            return "0004"

    class _FakeEngine:
        def connect(self):
            return _Fake()

        def dispose(self):
            pass

    monkeypatch.setattr("src.api.runtime.create_engine", lambda url, **k: _FakeEngine())
    with pytest.raises(RuntimeError, match="0005"):
        require_postgres_ready("postgresql+psycopg2://x/x", required_version="0005")
