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
    ApprovalRow,
    AuditRow,
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
        # 重建全部表（含 CHECK 约束），保证用例确定性
        for table in ("audit_events", "idempotency_records", "approval_decisions",
                      "refund_operations", "tickets", "orders", "policies", "order_items", "entity_seq"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn.execute(text(_SCHEMA))
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


def test_live_db_constraint_rejects_invalid_amount():
    """数据库独立拒绝非法金额（amount<=0 或负支付金额）。"""
    from sqlalchemy.exc import IntegrityError
    repo = PostgresAfterSalesRepository(DATABASE_URL)
    repo.insert_order(_order())
    repo.insert_ticket(TicketRow("tenant-a", "TKT-1", "ORD-1", "C1", "refund", "破损", "open"))
    with pytest.raises(IntegrityError):
        repo.insert_operation(_op("OP-BAD", "-5.00"))     # 负金额 → CHECK 拦截
    with pytest.raises(IntegrityError):
        repo.insert_operation(_op("OP-ZERO", "0.00"))     # 零金额 → CHECK 拦截


def test_live_db_constraint_rejects_invalid_status():
    """数据库独立拒绝非法状态（CHECK 状态枚举）。"""
    from sqlalchemy.exc import IntegrityError
    repo = PostgresAfterSalesRepository(DATABASE_URL)
    repo.insert_order(_order())
    repo.insert_ticket(TicketRow("tenant-a", "TKT-1", "ORD-1", "C1", "refund", "破损", "open"))
    bad = _op("OP-BAD-STATUS", "10.00")
    bad = OperationRow(bad.tenant_id, bad.operation_id, bad.ticket_id, bad.order_id,
                       bad.op_type, bad.amount, "warped", bad.idempotency_key, bad.created_by)
    with pytest.raises(IntegrityError):
        repo.insert_operation(bad)


def test_live_concurrent_same_idem_key_single_winner():
    """数据库幂等唯一约束：并发同 (tenant, key) 插入恰一个成功、另一个 UniqueViolation。"""
    repo = PostgresAfterSalesRepository(DATABASE_URL)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait()
            PostgresAfterSalesRepository(DATABASE_URL).insert_idem(
                IdemRow("tenant-a", "same-key", "h1", "OP-1"))
            with lock:
                outcomes.append("ok")
        except UniqueViolation:
            with lock:
                outcomes.append("conflict")
        except Exception:  # noqa: BLE001
            with lock:
                outcomes.append("error")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["conflict", "ok"]
    assert repo.get_idem("tenant-a", "same-key").payload_hash == "h1"


# ---------- unit_of_work：命令级原子写（同一事务跨表；无部分提交） ----------

def test_live_unit_of_work_atomic_commit_multitable(repo):
    """作用域内 订单+工单+操作+审计+审批+幂等 一次提交全部可见（真实单事务）。"""
    with repo.unit_of_work():
        repo.insert_order(_order())
        repo.insert_ticket(TicketRow("tenant-a", "TKT-1", "ORD-1", "C1", "refund", "破损", "open"))
        repo.insert_operation(_op("OP-1", "60.00"))
        repo.insert_audit(AuditRow("tenant-a", "create_refund", "operation", "OP-1",
                                   "agent", None, "draft", idempotency_key="k-OP-1"))
        repo.insert_approval(ApprovalRow("tenant-a", "OP-1", "approved", "同意", "approver", 1))
        repo.insert_idem(IdemRow("tenant-a", "k-OP-1", "h1", "OP-1"))
    assert repo.get_order("tenant-a", "ORD-1").paid_amount == Decimal("100.00")
    assert repo.get_ticket("tenant-a", "TKT-1").status == "open"
    assert repo.get_operation("tenant-a", "OP-1").status == "approved"
    assert len(repo.audit_of("tenant-a", "operation", "OP-1")) == 1
    assert len(repo.approvals_of("tenant-a", "OP-1")) == 1
    assert repo.get_idem("tenant-a", "k-OP-1").refund_id == "OP-1"


def test_live_unit_of_work_rollback_no_partial_commit(repo):
    """作用域内中途异常 → 整单位回滚：任何表都无部分写入（无部分提交）。"""
    with pytest.raises(ValueError):
        with repo.unit_of_work():
            repo.insert_order(_order())
            repo.insert_audit(AuditRow("tenant-a", "create_ticket", "ticket", "TKT-1",
                                       "agent", None, "open"))
            raise ValueError("模拟命令中途失败")
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        orders = conn.execute(text("SELECT COUNT(*) FROM orders")).scalar()
        audits = conn.execute(text("SELECT COUNT(*) FROM audit_events")).scalar()
    engine.dispose()
    assert orders == 0 and audits == 0


def test_live_unit_of_work_scope_reads_own_writes(repo):
    """作用域内读复用同一连接：可读到本事务未提交写入（同命令内校验需要）。"""
    with repo.unit_of_work():
        repo.insert_order(_order())
        repo.insert_ticket(TicketRow("tenant-a", "TKT-1", "ORD-1", "C1", "refund", "破损", "open"))
        assert repo.get_order("tenant-a", "ORD-1") is not None      # 未提交也可读
        assert repo.get_ticket("tenant-a", "TKT-1").status == "open"
    # 作用域外（新连接）同样可见（已提交）
    assert repo.get_order("tenant-a", "ORD-1") is not None


def test_live_unit_of_work_nested_rejected(repo):
    """不允许嵌套：作用域内再次进入 → RuntimeError（防事务错乱）。"""
    with pytest.raises(RuntimeError):
        with repo.unit_of_work():
            with repo.unit_of_work():
                pass
