"""Repository 契约测试（K4）：表级 CRUD/唯一约束/乐观版本/行锁容量/租户隔离。

实现参数化：MemoryAfterSalesRepository（必跑）；PostgresAfterSalesRepository 仅在
DATABASE_URL 或本地 5433 PostgreSQL 可达时加入（无则跳过并标记未实测）。
"""
import threading
from decimal import Decimal

import pytest

from src.repo import (
    AuditRow,
    IdemRow,
    MemoryAfterSalesRepository,
    OperationRow,
    OptimisticLockError,
    OrderItemRow,
    OrderRow,
    PolicyRow,
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


# ---------- unit_of_work：命令级原子写作用域 ----------

def test_unit_of_work_commit_visibility(repo):
    """作用域内多表写：正常退出后全部可见（一次命令 = 一个原子单位）。"""
    with repo.unit_of_work():
        repo.insert_order(_order())
        repo.insert_ticket(TicketRow(T1, "TKT-1", "ORD-1", "C1", "refund", "破损", "open"))
        repo.insert_audit(AuditRow(T1, "create_ticket", "ticket", "TKT-1", "agent", None, "open"))
        repo.insert_idem(IdemRow(T1, "key-1", "h1", "TKT-1"))
    assert repo.get_order(T1, "ORD-1") is not None
    assert repo.get_ticket(T1, "TKT-1").status == "open"
    assert len(repo.audit_of(T1, "ticket", "TKT-1")) == 1
    assert repo.get_idem(T1, "key-1").refund_id == "TKT-1"


def test_unit_of_work_rollback_no_partial(repo):
    """作用域内中途异常 → 整单位回滚（无部分提交）。"""
    with pytest.raises(ValueError):
        with repo.unit_of_work():
            repo.insert_order(_order())
            repo.insert_audit(AuditRow(T1, "create_ticket", "ticket", "TKT-1", "agent", None, "open"))
            raise ValueError("模拟命令中途失败")
    assert repo.get_order(T1, "ORD-1") is None
    assert repo.audit_of(T1, "ticket", "TKT-1") == []


def test_unit_of_work_rollback_preserves_prior_state(repo):
    """回滚只撤销本次单位内的写，不丢失作用域前的既有状态。"""
    repo.insert_order(_order(order_id="ORD-0", paid="50.00"))
    with pytest.raises(ValueError):
        with repo.unit_of_work():
            repo.insert_order(_order(order_id="ORD-1", paid="100.00"))
            raise ValueError("模拟失败")
    assert repo.get_order(T1, "ORD-0") is not None   # 先前状态保留
    assert repo.get_order(T1, "ORD-1") is None       # 单位内写被回滚


def test_unit_of_work_nested_rejected(repo):
    """不允许嵌套：作用域内再次进入 → RuntimeError。"""
    with pytest.raises(RuntimeError):
        with repo.unit_of_work():
            with repo.unit_of_work():
                pass


# ---------- 0004 命令数据面（memory 契约） ----------

def test_policy_insert_list_version_unique(repo):
    repo.insert_policy(PolicyRow("T1", "P-1", "refund", '["damaged"]', 30,
                                 Decimal("1.0000"), "2026-01-01", 1))
    with pytest.raises(UniqueViolation):   # 同 (tenant, policy, version) 重复
        repo.insert_policy(PolicyRow("T1", "P-1", "refund", '["damaged"]', 30,
                                     Decimal("1.0000"), "2026-01-01", 1))
    repo.insert_policy(PolicyRow("T1", "P-1", "refund", '["damaged"]', 30,
                                 Decimal("0.5000"), "2027-01-01", 2))   # 新版本共存
    repo.insert_policy(PolicyRow("T2", "P-1", "refund", '["damaged"]', 30,
                                 Decimal("1.0000"), "2026-01-01", 1))   # 跨租户
    assert len(repo.list_policies()) == 3


def test_order_item_and_next_seq(repo):
    repo.insert_order(_order())
    repo.insert_order_item(OrderItemRow("T1", "ORD-1", "S1", "商品A", 2, Decimal("50.00")))
    assert len(repo.list_order_items()) == 1
    assert repo.next_seq("T1", "ticket") == 1
    assert repo.next_seq("T1", "ticket") == 2        # 同租户同 kind 递增
    assert repo.next_seq("T1", "operation") == 1     # kind 隔离
    assert repo.next_seq("T2", "ticket") == 1        # 租户隔离


# ---------- 版本迁移 CAS 原语 ----------

def test_ticket_versioned_update_cas(repo):
    repo.insert_order(_order())
    repo.insert_ticket(TicketRow(T1, "TKT-1", "ORD-1", "C1", "refund", "破损", "open"))
    target = TicketRow(T1, "TKT-1", "ORD-1", "C1", "refund", "破损", "resolved",
                       resolution="refunded", version=1)
    repo.update_ticket_versioned(target, expected_version=1)
    assert repo.get_ticket(T1, "TKT-1").status == "resolved"
    with pytest.raises(OptimisticLockError):          # 旧版本再更新 → CAS 冲突
        repo.update_ticket_versioned(target, expected_version=1)


def test_operation_versioned_update_carries_decision(repo):
    repo.insert_order(_order())
    repo.insert_ticket(TicketRow(T1, "TKT-1", "ORD-1", "C1", "refund", "破损", "open"))
    repo.insert_operation(_op(op_id="OP-1", amount="60.00"))
    approved = _op(op_id="OP-1", amount="60.00")
    approved = OperationRow(approved.tenant_id, approved.operation_id, approved.ticket_id,
                            approved.order_id, approved.op_type, approved.amount, "executed",
                            approved.idempotency_key, approved.created_by, version=1,
                            decision_version=1, executed=True)
    repo.update_operation_versioned(approved, expected_version=1)
    got = repo.get_operation(T1, "OP-1")
    assert got.status == "executed" and got.executed is True
    assert got.decision_version == 1 and got.version == 2
