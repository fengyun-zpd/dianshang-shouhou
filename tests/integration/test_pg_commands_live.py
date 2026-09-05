"""PG-first 命令服务真实 PostgreSQL 集成测试（阶段三第五步收口）。

前置：opspilot-pg（5433）或 DATABASE_URL；不可达整模块 skip。
覆盖：八命令单事务链（SQL 断言业务/幂等/审计同落）、失败整事务回滚零残留、
两连接并发 approve CAS 恰一成功、同订单并发 execute 容量恰一成功。
"""
import os
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    ApproveCommand,
    CloseTicketCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    RequestType,
    ReconcileCommand,
    Role,
    SubmitCommand,
)
from src.domain.after_sales.pg_commands import PgCommandService
from src.repo import (
    OrderRow,
    PolicyRow,
    PostgresAfterSalesRepository,
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
    not _pg_available(), reason="PostgreSQL 不可达：数据库集成未实测")


@pytest.fixture
def service():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        for table in ("audit_events", "idempotency_records", "approval_decisions",
                      "refund_operations", "tickets", "orders", "policies",
                      "order_items", "entity_seq", "workflow_threads"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn.execute(text(_SCHEMA))
    engine.dispose()
    repo = PostgresAfterSalesRepository(DATABASE_URL)
    repo.insert_order(OrderRow("T1", "ORD-1", "C1", "delivered", Decimal("100.00"), 2))
    repo.insert_policy(PolicyRow("T1", "P-1", "refund", '["damaged"]', 30,
                                 Decimal("1.0000"), "1970-01-01", 1))
    return PgCommandService(repo)


def _ticket_cmd(key="tk-1"):
    return CreateTicketCommand("T1", "ORD-1", "C1", RequestType.REFUND,
                               "商品破损", ("damaged",), Role.AGENT, key)


def _op_ids():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        ids = [r[0] for r in conn.execute(text(
            "SELECT operation_id FROM refund_operations ORDER BY operation_id"))]
    engine.dispose()
    return ids


def _sql_one(statement, params=None):
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        v = conn.execute(text(statement), params or {}).scalar()
    engine.dispose()
    return v


def _version(operation_id):
    return _sql_one("SELECT version FROM refund_operations WHERE operation_id=:o",
                    {"o": operation_id})


def _approve(service, op_id):
    service.approve("T1", ApproveCommand(op_id, Role.APPROVER,
                                         decision_version=_version(op_id)))


def test_live_eight_command_chain_sql_verified(service):
    """建单→草稿→提交→审批→执行→unknown 对账→关单 全链单事务；SQL 断言各表行。"""
    t = service.create_ticket(_ticket_cmd("tk-e2e"))
    assert t.ticket_id == "TKT-00001"
    op = service.create_refund_draft("T1", CreateRefundCommand("TKT-00001", Decimal("60.00"),
                                                               "破损", Role.AGENT, "k-e2e"))
    service.submit("T1", SubmitCommand(op.operation_id, Role.AGENT))
    _approve(service, op.operation_id)
    svc_exec = service.execute("T1", ExecuteCommand(op.operation_id, Role.SYSTEM, "timeout"))
    assert svc_exec.status.value == "unknown"
    done = service.reconcile("T1", ReconcileCommand(op.operation_id, Role.SYSTEM, "success"))
    assert done.status.value == "executed"
    closed = service.close_ticket("T1", CloseTicketCommand("TKT-00001", Role.AGENT))
    assert closed.status.value == "closed" and closed.resolution == "refunded"

    assert _sql_one("SELECT COUNT(*) FROM tickets WHERE ticket_id='TKT-00001'") == 1
    assert _sql_one("SELECT status FROM refund_operations WHERE operation_id=:o",
                    {"o": op.operation_id}) == "executed"
    assert _sql_one("SELECT executed FROM refund_operations WHERE operation_id=:o",
                    {"o": op.operation_id}) is True
    assert _sql_one("SELECT decision_version FROM refund_operations WHERE operation_id=:o",
                    {"o": op.operation_id}) == 2
    assert _sql_one("SELECT COUNT(*) FROM idempotency_records WHERE idem_key IN "
                    "('T1:tk-e2e','T1:k-e2e')") == 2
    assert _sql_one("SELECT COUNT(*) FROM audit_events") >= 7   # 建/草稿/提交/审批/执行/对账/关单


def test_live_no_partial_commit_on_validation_failure(service):
    """审批过期版本失败 → 整事务回滚：状态/版本/审计零残留（无部分提交）。"""
    service.create_ticket(_ticket_cmd("tk-nc"))
    op = service.create_refund_draft("T1", CreateRefundCommand("TKT-00001", Decimal("60.00"),
                                                               "破损", Role.AGENT, "k-nc"))
    service.submit("T1", SubmitCommand(op.operation_id, Role.AGENT))
    with pytest.raises(AfterSalesError) as ei:
        service.approve("T1", ApproveCommand(op.operation_id, Role.APPROVER, decision_version=1))
    assert ei.value.code == AfterSalesErrorCode.DECISION_VERSION_MISMATCH
    assert _sql_one("SELECT status FROM refund_operations WHERE operation_id=:o",
                    {"o": op.operation_id}) == "pending_approval"
    assert _sql_one("SELECT COUNT(*) FROM audit_events WHERE action='approve'") == 0
    # 业务行仍存在（失败事务只回滚本次未提交写，不影响先前已提交的草稿/提交审计）
    assert _sql_one("SELECT COUNT(*) FROM audit_events WHERE action='submit'") == 1


def test_live_concurrent_approve_single_winner(service):
    """两连接同 expected_version 并发审批 → 恰一成功，另一转 DECISION_VERSION_MISMATCH。"""
    service.create_ticket(_ticket_cmd("tk-ca"))
    op = service.create_refund_draft("T1", CreateRefundCommand("TKT-00001", Decimal("60.00"),
                                                               "破损", Role.AGENT, "k-ca"))
    service.submit("T1", SubmitCommand(op.operation_id, Role.AGENT))
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait()
            s = PgCommandService(PostgresAfterSalesRepository(DATABASE_URL))
            s.approve("T1", ApproveCommand(op.operation_id, Role.APPROVER, decision_version=2))
            with lock:
                outcomes.append("ok")
        except AfterSalesError as e:
            with lock:
                outcomes.append(e.code.value)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["AFTER_SALES_DECISION_VERSION_MISMATCH", "ok"]
    assert _sql_one("SELECT status FROM refund_operations WHERE operation_id=:o",
                    {"o": op.operation_id}) == "approved"
    assert _sql_one("SELECT COUNT(*) FROM audit_events WHERE action='approve'") == 1


def test_live_concurrent_execute_capacity_single_winner(service):
    """同订单两张已批准 60：两连接并发 execute → 恰一成功，另一容量拒绝（累计≤实付）。"""
    service.create_ticket(_ticket_cmd("tk-ce"))
    op1 = service.create_refund_draft("T1", CreateRefundCommand("TKT-00001", Decimal("60.00"),
                                                                "破损", Role.AGENT, "k-e1"))
    op2 = service.create_refund_draft("T1", CreateRefundCommand("TKT-00001", Decimal("60.00"),
                                                                "破损", Role.AGENT, "k-e2"))
    for o in (op1, op2):
        service.submit("T1", SubmitCommand(o.operation_id, Role.AGENT))
        _approve(service, o.operation_id)
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(op_id):
        try:
            barrier.wait()
            s = PgCommandService(PostgresAfterSalesRepository(DATABASE_URL))
            s.execute("T1", ExecuteCommand(op_id, Role.SYSTEM, "success"))
            with lock:
                outcomes.append("ok")
        except AfterSalesError as e:
            with lock:
                outcomes.append(e.code.value)

    threads = [threading.Thread(target=worker, args=(o.operation_id,)) for o in (op1, op2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["AFTER_SALES_AMOUNT_EXCEEDS_REMAINING", "ok"]
    total = _sql_one("SELECT COALESCE(SUM(amount),0) FROM refund_operations"
                     " WHERE status='executed' AND order_id='ORD-1'")
    assert Decimal(str(total)) == Decimal("60.00")
    assert _sql_one("SELECT COUNT(*) FROM refund_operations WHERE status='executed'") == 1
