"""K4：PostgreSQL Repository 集成测试（真实数据库实测）。

前置：本机 PostgreSQL 容器（postgres:16-alpine，端口 5433，user/pw/db=opspilot）
或环境变量 DATABASE_URL（sqlalchemy url）。不可达时整模块 skip，并明确标注“数据库集成未实测”。
"""
import os
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.repo import (
    IdemRow,
    OperationRow,
    OptimisticLockError,
    OrderRow,
    PostgresAfterSalesRepository,
    TicketRow,
    UniqueViolation,
)

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
    not _pg_available(),
    reason="PostgreSQL 不可达：数据库集成未实测（仅契约/memory 已验证）",
)


@pytest.fixture(autouse=True)
def _clean_db():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text(_SCHEMA))  # CREATE TABLE IF NOT EXISTS（幂等）
        for table in ("approval_decisions", "audit_events", "idempotency_records",
                      "refund_operations", "tickets", "orders"):
            conn.execute(text(f"DELETE FROM {table}"))
    engine.dispose()


@pytest.fixture
def repo() -> PostgresAfterSalesRepository:
    return PostgresAfterSalesRepository(DATABASE_URL)


def _order(paid="100.00", order_id="ORD-1") -> OrderRow:
    return OrderRow("tenant-a", order_id, "C1", "delivered", Decimal(paid), 2)


def _op(op_id: str, amount: str) -> OperationRow:
    return OperationRow("tenant-a", op_id, "TKT-1", "ORD-1", "refund",
                        Decimal(amount), "approved", f"k-{op_id}", "agent")


def test_live_crud_roundtrip(repo):
    repo.insert_order(_order())
    repo.insert_ticket(TicketRow("tenant-a", "TKT-1", "ORD-1", "C1", "refund", "破损", "open"))
    repo.insert_operation(_op("OP-1", "60.00"))
    assert repo.get_order("tenant-a", "ORD-1").paid_amount == Decimal("100.00")
    assert repo.get_ticket("tenant-a", "TKT-1").status == "open"
    assert repo.get_operation("tenant-a", "OP-1").amount == Decimal("60.00")


def test_live_idempotency_unique_violation(repo):
    repo.insert_idem(IdemRow("tenant-a", "key-1", "h1", "OP-1"))
    with pytest.raises(UniqueViolation):
        repo.insert_idem(IdemRow("tenant-a", "key-1", "h2", "OP-2"))
    assert repo.get_idem("tenant-a", "key-1").payload_hash == "h1"


def test_live_optimistic_version_conflict(repo):
    repo.insert_order(_order())
    repo.update_order_versioned(_order(), expected_version=1)
    with pytest.raises(OptimisticLockError):
        repo.update_order_versioned(_order(), expected_version=1)


def test_live_capacity_execution(repo):
    repo.insert_order(_order(paid="100.00"))
    repo.insert_ticket(TicketRow("tenant-a", "TKT-1", "ORD-1", "C1", "refund", "破损", "open"))
    assert repo.try_execute_refund("tenant-a", "ORD-1", _op("OP-1", "60.00")) is True
    assert repo.executed_sum_for_order("tenant-a", "ORD-1") == Decimal("60.00")
    assert repo.try_execute_refund("tenant-a", "ORD-1", _op("OP-2", "60.00")) is False
    assert repo.executed_sum_for_order("tenant-a", "ORD-1") == Decimal("60.00")


def test_live_concurrent_execute_single_success_by_row_lock(repo):
    """真实 PostgreSQL 行锁：同订单两并发事务各执行 60.00 → 恰一个成功，累计 ≤ 实付。"""
    repo.insert_order(_order(paid="100.00"))
    repo.insert_ticket(TicketRow("tenant-a", "TKT-1", "ORD-1", "C1", "refund", "破损", "open"))
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(op_id: str):
        try:
            barrier.wait()
            ok = PostgresAfterSalesRepository(DATABASE_URL).try_execute_refund(
                "tenant-a", "ORD-1", _op(op_id, "60.00"))
        except Exception as e:  # noqa: BLE001
            ok = False
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker, args=(f"OP-{i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    assert repo.executed_sum_for_order("tenant-a", "ORD-1") == Decimal("60.00")


def test_live_unknown_only_original_key_semantics():
    """unknown 收口只能原键：幂等记录唯一约束下，原键命中返回原记录（换新键必然冲突或新对象由领域层拒绝）。"""
    repo = PostgresAfterSalesRepository(DATABASE_URL)
    repo.insert_idem(IdemRow("tenant-a", "orig-key", "h", "OP-UNKNOWN"))
    got = repo.get_idem("tenant-a", "orig-key")
    assert got is not None and got.refund_id == "OP-UNKNOWN"
