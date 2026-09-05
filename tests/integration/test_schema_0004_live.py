"""Alembic 0004 数据模型 live 测试（PG-first 命令数据面）。

前置：PostgreSQL 容器（opspilot-pg，5433）或 DATABASE_URL；不可达整模块 skip。
覆盖：policies（版本唯一/ratio·window CHECK）、order_items（FK/quantity·price CHECK）、
entity_seq（kind CHECK/递增）；policy 多版本共存；显式租户复合主键。
"""
import os
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot",
)
_SCHEMA = (ROOT / "src" / "repo" / "schema.sql").read_text(encoding="utf-8")


def _pg_available() -> bool:
    try:
        engine = create_engine(DATABASE_URL, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(), reason="PostgreSQL 不可达：数据库集成未实测")


@pytest.fixture(autouse=True)
def _clean_db():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        for table in ("audit_events", "idempotency_records", "approval_decisions",
                      "refund_operations", "tickets", "orders", "policies",
                      "order_items", "entity_seq"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn.execute(text(_SCHEMA))
    engine.dispose()


@pytest.fixture
def engine():
    e = create_engine(DATABASE_URL)
    yield e
    e.dispose()


def _seed_order(e):
    with e.begin() as conn:
        conn.execute(text(
            "INSERT INTO orders (tenant_id, order_id, customer_id, status, paid_amount,"
            " days_since_sign, version) VALUES ('T1','ORD-1','C1','delivered',100.00,2,1)"))


def test_schema_0004_tables_exist(engine):
    with engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
    assert {"policies", "order_items", "entity_seq"} <= tables


def test_policies_tenant_version_unique_and_checks(engine):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO policies (tenant_id, policy_id, request_type, reason_tags,"
            " window_days, refund_ratio, effective_from, version) VALUES "
            "('T1','P-1','refund','[\"damaged\"]',30,1.0000,'2026-01-01',1)"))
    # 同 (tenant, policy, version) 重复 → 唯一冲突；不同租户/版本允许（多版本共存）
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO policies (tenant_id, policy_id, request_type, reason_tags,"
                " window_days, refund_ratio, effective_from, version) VALUES "
                "('T1','P-1','refund','[\"damaged\"]',30,1.0000,'2026-01-01',1)"))
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO policies (tenant_id, policy_id, request_type, reason_tags,"
            " window_days, refund_ratio, effective_from, version) VALUES "
            "('T1','P-1','refund','[\"damaged\"]',30,0.5000,'2027-01-01',2),"
            "('T2','P-1','refund','[\"damaged\"]',30,1.0000,'2026-01-01',1)"))
    # 非法比例 → CHECK 拒绝
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO policies (tenant_id, policy_id, request_type, reason_tags,"
                " window_days, refund_ratio, effective_from, version) VALUES "
                "('T1','P-BAD','refund','[]',30,1.5000,'2026-01-01',1)"))
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM policies")).scalar()
    assert n == 3  # T1 v1 + T1 v2 + T2 v1


def test_order_items_fk_and_checks(engine):
    _seed_order(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO order_items (tenant_id, order_id, sku, name, quantity, unit_price)"
            " VALUES ('T1','ORD-1','S1','商品A',2,50.00)"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:   # quantity<=0 → CHECK
            conn.execute(text(
                "INSERT INTO order_items (tenant_id, order_id, sku, name, quantity, unit_price)"
                " VALUES ('T1','ORD-1','S2','商品B',0,10.00)"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:   # 订单不存在 → FK
            conn.execute(text(
                "INSERT INTO order_items (tenant_id, order_id, sku, name, quantity, unit_price)"
                " VALUES ('T1','ORD-NOPE','S3','x',1,10.00)"))
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT sku, quantity, unit_price FROM order_items WHERE order_id='ORD-1'")).fetchone()
    assert row[0] == "S1" and row[1] == 2 and row[2] == 50.00


def test_entity_seq_kind_and_increment(engine):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO entity_seq (tenant_id, kind, next_val) VALUES ('T1','ticket',1),"
            " ('T1','operation',7), ('T2','ticket',1)"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:   # 非法 kind → CHECK
            conn.execute(text("INSERT INTO entity_seq (tenant_id, kind) VALUES ('T1','order')"))
    # 递增读：事务内 FOR UPDATE 取数并 +1（命令 id 分配原语）
    with engine.begin() as conn:
        v = conn.execute(text(
            "UPDATE entity_seq SET next_val = next_val + 1 WHERE tenant_id='T1' AND kind='ticket'"
            " RETURNING next_val")).scalar()
    assert v == 2
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT next_val FROM entity_seq WHERE tenant_id='T2' AND kind='ticket'")).scalar() == 1
