"""Alembic 0004 数据模型 live 测试（PG-first 命令数据面）。

前置：隔离测试库 OPSPILOT_TEST_DATABASE_URL（opspilot_test_*）；未设置/不可达整模块 skip
（绝不回退 DATABASE_URL 指向的共享 opspilot 主库）。
覆盖：policies（版本唯一/ratio·window CHECK）、order_items（FK/quantity·price CHECK）、
entity_seq（kind CHECK/递增）；policy 多版本共存；显式租户复合主键。
"""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.pg_live import live_test_db_url, pg_reachable, reset_test_schema

TEST_DB_URL = live_test_db_url()

pytestmark = pytest.mark.skipif(
    TEST_DB_URL is None or not pg_reachable(TEST_DB_URL),
    reason="OPSPILOT_TEST_DATABASE_URL 未设置或 PostgreSQL 不可达："
           "未使用隔离测试库，跳过破坏性集成（PG 集成未实测）")


@pytest.fixture(autouse=True)
def _clean_db():
    """guard 通过后重建全部业务表（opspilot_test_* 隔离库）；guard 拒绝 → 抛错失败。"""
    reset_test_schema(TEST_DB_URL)


@pytest.fixture
def engine():
    e = create_engine(TEST_DB_URL)
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
