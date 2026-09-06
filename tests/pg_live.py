"""破坏性 PG live 集成测试共享入口（V1 验收：统一 9 个 live 文件的隔离库策略）。

策略（fail-closed，不回退 DATABASE_URL）：
- 破坏性集成唯一允许的目标：``OPSPILOT_TEST_DATABASE_URL``（指向 ``opspilot_test_*``
  隔离库）。未设置或不可达 → 整模块 skip（"PG 集成未实测"，如实标注，不伪造通过）；
- 重建统一经 ``src.platform.pg_test_guard.reset_test_schema``：guard 拒绝
  （非 opspilot_test_*@localhost，例如误设 DATABASE_URL 指向共享 opspilot 主库）→
  抛 TestDbGuardError 使测试失败，绝不悄悄指向主库执行 DROP；
- live 文件使用方式：

    from tests.pg_live import live_test_db_url, pg_reachable, reset_test_schema

    TEST_DB_URL = live_test_db_url()
    pytestmark = pytest.mark.skipif(
        TEST_DB_URL is None or not pg_reachable(TEST_DB_URL),
        reason="OPSPILOT_TEST_DATABASE_URL 未设置/不可达：跳过破坏性集成（PG 集成未实测）")

    @pytest.fixture(autouse=True)
    def _clean_db():
        reset_test_schema(TEST_DB_URL)
"""
from __future__ import annotations

import os
from typing import Optional

import pytest
from sqlalchemy import create_engine, text

from src.platform.pg_test_guard import (
    reset_test_schema,  # noqa: F401  (live 文件统一从此处引用)
    require_isolated_test_db,
    test_database_url_from_env,
)

SKIP_REASON = ("OPSPILOT_TEST_DATABASE_URL 未设置或 PostgreSQL 不可达："
               "未使用隔离测试库（opspilot_test_*），跳过破坏性集成（PG 集成未实测）")


def live_test_db_url() -> Optional[str]:
    """破坏性 live 测试唯一允许的 DROP/重建目标（不回退 DATABASE_URL）。"""
    return test_database_url_from_env()


def pg_reachable(url: Optional[str]) -> bool:
    """PostgreSQL 可达性探测（3s 超时）；url 为空 → False。

    显式提供的 URL 先过隔离守卫；非法测试库配置必须失败，不能被吞成 skip，
    也不能在 guard 前执行任何连接探测。
    """
    if not url:
        return False
    require_isolated_test_db(url)
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001  不可达/权限不足 → 如实判定不可用
        return False


def live_pg_skipif(reason: str = SKIP_REASON):
    """模块/用例级 skipif 装饰器：URL 未设置或不可达时跳过（guard 拒绝不在此列）。"""
    url = live_test_db_url()
    return pytest.mark.skipif(url is None or not pg_reachable(url), reason=reason)


def guard_require(url: Optional[str]) -> str:
    """fixture 内守卫入口：显式设置的 URL 必须通过 guard，否则抛错失败（fail-closed）。"""
    if url is None:
        raise RuntimeError("OPSPILOT_TEST_DATABASE_URL 未设置，破坏性集成不应执行（应已 skip）")
    return require_isolated_test_db(url)
