"""Runtime profile（阶段四第 3 节）：显式选择 memory 或 pg 业务后端。

纪律：
- memory profile：仅用于本地演示/测试（内存 AfterSalesService + 合成 seed）；
- pg profile：必须满足 PostgreSQL 可达 + Alembic 迁移 >= 期望版本 + 健康探测，否则
  **启动失败并报错，绝不静默降级到内存**；
- 命令层（PgCommandService）为 pg profile 的业务事实源；API、Gateway 和 Runner
  只依赖 AfterSalesApplicationPort，本模块负责启动装配与校验。
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Optional

from sqlalchemy import create_engine, text


class RuntimeProfile(str, Enum):
    MEMORY = "memory"
    PG = "pg"


REQUIRED_SCHEMA_VERSION = "0006"   # 稳定审计 event_id（含 0005 工作流租约）


def default_pg_url() -> Optional[str]:
    return os.environ.get("DATABASE_URL") or None


def probe_postgres(url: str) -> bool:
    """健康探测：SELECT 1（3s 超时）。"""
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


def require_postgres_ready(url: str, required_version: str = REQUIRED_SCHEMA_VERSION) -> str:
    """pg profile 装配门禁：数据库可达 + alembic 版本达标；不满足 → RuntimeError（不 fallback）。"""
    if not probe_postgres(url):
        raise RuntimeError(
            f"pg profile 启动失败：PostgreSQL 不可达（{url}）；拒绝静默降级到内存。"
            "请启动数据库（docs/POSTGRES.md）后重试。")
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        engine.dispose()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"pg profile 启动失败：无法读取 alembic_version（{e}）") from e
    if version != required_version:
        raise RuntimeError(
            f"pg profile 启动失败：数据库 schema 版本 {version!r} != 期望 {required_version!r}；"
            "请先执行 `python -m alembic -c alembic.ini upgrade head`。拒绝静默降级。")
    return url


def build_pg_command_service(url: str):
    """pg profile 命令服务装配（迁移/健康已由 require_postgres_ready 门禁保证）。"""
    from src.domain.after_sales.pg_commands import PgCommandService
    from src.repo import PostgresAfterSalesRepository
    require_postgres_ready(url)
    return PgCommandService(PostgresAfterSalesRepository(url))
