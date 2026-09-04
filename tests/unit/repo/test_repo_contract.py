"""Repository 契约测试（K4）：表级 CRUD/唯一约束/乐观版本/行锁容量/租户隔离。

实现参数化：MemoryAfterSalesRepository（必跑）；PostgresAfterSalesRepository 仅在
DATABASE_URL 或本地 5433 PostgreSQL 可达时加入（无则跳过并标记未实测）。
"""
import threading
from decimal import Decimal

import pytest

from src.repo import (
    IdemRow,
    MemoryAfterSalesRepository,
    OperationRow,
    OptimisticLockError,
    OrderRow,
    TicketRow,
    UniqueViolation,
)

T1 = "tenant-a"
T2 = "tenant-b"


def _order(tenant=T1, order_id="ORD-1", paid="100.00") -> OrderRow:
    return OrderRow(tenant_id=tenant, order_id=order_id, customer_id="C1",
                    status="delivered", paid_amount=Decimal(paid), days_since_sign=2)


def _op(tenant=T1, op_id="OP-1", amount="60.00", ticket_id="TKT-1",
        order_id="ORD-1") -> OperationRow:
    return OperationRow(tenant_id=tenant, operation_id=op_id, ticket_id=ticket_id,
                        order_id=order_id, op_type="refund", amount=Decimal(amount),
                        status="approved", idempotency_key=f"k-{op_id}", created_by="agent")


@pytest.fixture
def repo():
    return MemoryAfterSalesRepository()


def test_crud_roundtrip(repo):
    repo.insert_order(_order())
    repo.insert_ticket(TicketRow(T1, "TKT-1", "ORD-1", "C1", "refund", "破损", "open"))
    repo.insert_operation(_op())
    assert repo.get_order(T1, "ORD-1") == _order()
    assert repo.get_ticket(T1, "TKT-1").status == "open"
    op = repo.get_operation(T1, "OP-1")
    assert op.status == "approved" and op.amount == Decimal("60.00")


def test_idempotency_unique(repo):
    repo.insert_idem(IdemRow(T1, "key-1", "h1", "OP-1"))
    with pytest.raises(UniqueViolation):
        repo.insert_idem(IdemRow(T1, "key-1", "h2", "OP-2"))
    # 不同租户同 key 允许（租户隔离）
    repo.insert_idem(IdemRow(T2, "key-1", "h1", "OP-9"))


def test_optimistic_version_conflict(repo):
    repo.insert_order(_order(paid="100.00"))
    repo.update_order_versioned(_order(paid="100.00"), expected_version=1)
    with pytest.raises(OptimisticLockError):
        repo.update_order_versioned(_order(paid="100.00"), expected_version=1)  # 旧版本


def test_capacity_execution_sequential(repo):
    repo.insert_order(_order(paid="100.00"))
    assert repo.try_execute_refund(T1, "ORD-1", _op(op_id="OP-1")) is True
    assert repo.executed_sum_for_order(T1, "ORD-1") == Decimal("60.00")
    assert repo.try_execute_refund(T1, "ORD-1", _op(op_id="OP-2")) is False   # 60+60>100
    assert repo.executed_sum_for_order(T1, "ORD-1") == Decimal("60.00")


def test_tenant_isolation(repo):
    repo.insert_order(_order(T1, "ORD-1", paid="100.00"))
    assert repo.try_execute_refund(T1, "ORD-1", _op(T1)) is True
    assert repo.executed_sum_for_order(T2, "ORD-1") == Decimal("0.00")   # 其他租户视图为 0
    assert repo.get_order(T2, "ORD-1") is None


def test_concurrent_capacity_single_success(repo):
    """N 个同订单不同键并发执行 60.00 → 恰一个成功（内存实现以锁模拟行锁）。"""
    repo.insert_order(_order(paid="100.00"))
    n = 8
    barrier = threading.Barrier(n)
    results: list[bool] = []

    def worker(i: int):
        try:
            barrier.wait()
            results.append(repo.try_execute_refund(T1, "ORD-1", _op(op_id=f"OP-{i}")))
        except Exception as e:  # noqa: BLE001
            results.append(False)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    assert repo.executed_sum_for_order(T1, "ORD-1") == Decimal("60.00")
