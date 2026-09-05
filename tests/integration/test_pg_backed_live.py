"""PG-backed 领域会话（阶段二）真实 PostgreSQL 集成测试。

前置：本机 PostgreSQL 容器（opspilot-pg，5433）或 DATABASE_URL；不可达整模块 skip。
验证：业务事实确实落 PG 行表（直接 SQL 断言）、save→load 保真、save 失败整单位回滚
（无部分提交）、重启装载后重复请求无重复副作用、审计在重建实例上继续追加。
"""
import os
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.domain.after_sales import (
    ApproveCommand,
    CloseTicketCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    PolicyRule,
    RequestType,
    Role,
    SubmitCommand,
)
from src.persistence.pg_backed import PgBackedSession
from src.repo import PostgresAfterSalesRepository
from tests.unit.domain.after_sales.helpers import make_order

ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot",
)
_SCHEMA = (ROOT / "src" / "repo" / "schema.sql").read_text(encoding="utf-8")

POLS = [PolicyRule(policy_id="P-DAMAGED-FULL", tenant_id="T1",
                   request_type=RequestType.REFUND, reason_tags=("damaged",),
                   window_days=30, refund_ratio=Decimal("1.00"))]


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
        for table in ("audit_events", "idempotency_records", "approval_decisions",
                      "refund_operations", "tickets", "orders"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn.execute(text(_SCHEMA))
    engine.dispose()


@pytest.fixture
def session() -> PgBackedSession:
    return PgBackedSession(PostgresAfterSalesRepository(DATABASE_URL))


def _full_service():
    # baseline_service() 已 seed ORD-1(paid 100) + 破损全额政策
    return __import__("tests.unit.domain.after_sales.helpers",
                      fromlist=["baseline_service"]).baseline_service()


def _run_full_flow(svc):
    t = svc.create_ticket(CreateTicketCommand("T1", "ORD-1", "C1", RequestType.REFUND,
                                              "商品破损", ("damaged",), Role.AGENT, "tk-1"))
    op = svc.create_refund(CreateRefundCommand(t.ticket_id, Decimal("60.00"),
                                               "破损", Role.AGENT, "k-1"))
    svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    op = svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, 1))
    svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM, "success"))
    return svc


def _counts():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                for t in ("orders", "tickets", "refund_operations",
                          "approval_decisions", "idempotency_records", "audit_events")}
    engine.dispose()


def test_live_facts_really_in_postgres(session):
    """save 后业务事实以行表真实存在于 PostgreSQL（直接 SQL 断言）。"""
    svc = _run_full_flow(_full_service())
    session.save(svc)
    c = _counts()
    assert c["orders"] == 1 and c["tickets"] == 1 and c["refund_operations"] == 1
    assert c["audit_events"] >= 5          # 建单/草稿/提交/审批/执行审计
    assert c["approval_decisions"] == 1    # 审批流水已派生落库
    assert c["idempotency_records"] == 2   # tk-1 与 k-1
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        op_status = conn.execute(text(
            "SELECT status, amount, decision_version, executed FROM refund_operations")).fetchone()
        row_amount = conn.execute(text(
            "SELECT paid_amount FROM orders WHERE order_id='ORD-1'")).scalar()
    engine.dispose()
    assert op_status[0] == "executed" and op_status[1] == Decimal("60.00")
    assert op_status[2] == 1 and op_status[3] is True
    assert row_amount == Decimal("100.00")


def test_live_save_load_roundtrip(session):
    svc = _run_full_flow(_full_service())
    session.save(svc)
    svc2 = session.load(policies=POLS)
    st1, st2 = svc.export_state(), svc2.export_state()
    assert len(st2["tickets"]) == len(st1["tickets"]) == 1
    assert len(st2["operations"]) == len(st1["operations"]) == 1
    assert len(st2["audit"]) == len(st1["audit"])
    assert st2["refunded"] == st1["refunded"] == {"ORD-1": Decimal("60.00")}
    assert set(st2["idempotency"]) == set(st1["idempotency"])
    t2 = list(st2["tickets"].values())[0]
    assert t2.reason_tags == ("damaged",) and t2.created_by == Role.AGENT
    op2 = list(st2["operations"].values())[0]
    assert op2.executed is True and op2.decision_version == 1


def test_live_save_failure_rolls_back_whole_unit(session, monkeypatch):
    """save 中途失败 → 整单位回滚：先前已落库的事实不被部分覆盖（无部分提交）。"""
    svc = _run_full_flow(_full_service())
    session.save(svc)
    before = _counts()

    repo = session._repo
    original = repo.insert_operation

    def _boom(row):
        raise RuntimeError("模拟写操作中途失败")

    monkeypatch.setattr(repo, "insert_operation", _boom)
    svc2 = _run_full_flow(_full_service())   # 新状态（第二张工单/操作）
    with pytest.raises(RuntimeError):
        session.save(svc2)
    monkeypatch.setattr(repo, "insert_operation", original)

    after = _counts()
    assert after == before   # 清空与插入整体回滚，数据库仍为第一次成功保存的镜像


def test_live_reload_no_duplicate_side_effect_and_append_audit(session):
    """重启装载语义：load 后同键重复请求返回原结果；可继续关单且审计追加。"""
    svc = _run_full_flow(_full_service())
    session.save(svc)
    svc2 = session.load(policies=POLS)

    orig = list(svc.export_state()["tickets"].values())[0]
    dup = svc2.create_ticket(CreateTicketCommand("T1", "ORD-1", "C1", RequestType.REFUND,
                                                 "商品破损", ("damaged",), Role.AGENT, "tk-1"))
    assert dup.ticket_id == orig.ticket_id
    assert len(svc2.export_state()["tickets"]) == 1
    assert svc2.refunded_amount("ORD-1") == Decimal("60.00")

    n0 = len(svc2.export_state()["audit"])
    svc2.close_ticket(CloseTicketCommand(orig.ticket_id, Role.AGENT))
    assert len(svc2.export_state()["audit"]) > n0
    assert svc2.export_state()["tickets"][orig.ticket_id].status.value == "closed"

    session.save(svc2)                       # 关单后再次镜像
    svc3 = session.load(policies=POLS)
    assert svc3.export_state()["tickets"][orig.ticket_id].status.value == "closed"


def test_live_restart_resume_persistent_checkpoint_with_pg_facts(tmp_path, session):
    """完整重启链：工作流挂起于审批（checkpoint 落 SQLite）→ 业务事实 save 到 PG →
    “进程重启”后领域服务从 PG 重建（业务事实重读 PG）+ 同一持久 checkpoint resume →
    审批/执行正确完成且无重复副作用；SQL 直接断言 PG 行最终状态。"""
    from src.agents import WorkflowRunner
    from src.agents.checkpoint import open_sqlite_checkpointer
    from tests.unit.domain.after_sales.helpers import baseline_service

    svc1 = baseline_service()                      # ORD-1 paid 100 + 破损全额政策
    cp_db = tmp_path / "checkpoints.sqlite"
    cp1 = open_sqlite_checkpointer(str(cp_db))
    r1 = WorkflowRunner(svc1, checkpointer=cp1)
    pending = r1.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="restart-pg")
    assert pending.waiting_approval
    op_id = pending.state["operation_id"]
    session.save(svc1)                             # 业务事实（pending_approval）落 PG

    # “进程重启”：领域服务从 PG 行表重建 + 同一 SQLite 文件重开持久 checkpoint
    svc2 = session.load(policies=POLS)
    cp2 = open_sqlite_checkpointer(str(cp_db))
    r2 = WorkflowRunner(svc2, checkpointer=cp2)
    r2.submit_decision(op_id, "approved")          # 授权人员决定（作用于重建后的领域）
    final = r2.resume("restart-pg", tenant_id="T1")
    assert final.finished and final.outcome == "refunded"
    assert svc2.refunded_amount("ORD-1") == Decimal("100.00")
    assert svc2.get_operation(op_id).status.value == "executed"
    # 不重放：工单/操作仅各一；建单审计仅一条
    assert len(svc2.export_state()["tickets"]) == 1
    assert len(svc2.export_state()["operations"]) == 1
    assert len([e for e in svc2.audit_log() if e.action == "create_ticket"]) == 1

    session.save(svc2)                             # 执行后事实再镜像回 PG
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        op_status = conn.execute(text(
            "SELECT status, executed FROM refund_operations "
            "WHERE operation_id=:op"), {"op": op_id}).fetchone()
        n_audit = conn.execute(text("SELECT COUNT(*) FROM audit_events")).scalar()
    engine.dispose()
    assert op_status[0] == "executed" and op_status[1] is True
    assert n_audit >= 5
