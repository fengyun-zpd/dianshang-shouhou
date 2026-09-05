"""R1/R2：WorkflowRunner 内部强制 workflow_threads 租约（真实 PostgreSQL）测试。

覆盖：两独立 Runner（owner A/B）共享领域服务与 SQLite checkpoint、竞争同一 thread 的
resume —— 仅持约方可推进，失约方抛 ThreadLeaseError 且零业务副作用；
租约过期后新 owner 同 fingerprint 可接管；旧 owner 在他人持约期间再 resume 稳定拒绝；
完成后仅 owner 释放。
"""
import os
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.repo import PostgresAfterSalesRepository

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


pytestmark = pytest.mark.skipif(not _pg_available(),
                                reason="PostgreSQL 不可达：数据库集成未实测")


@pytest.fixture
def repo():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        for table in ("workflow_threads", "audit_events", "idempotency_records",
                      "approval_decisions", "refund_operations", "tickets", "orders",
                      "policies", "order_items", "entity_seq"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn.execute(text(_SCHEMA))
    engine.dispose()
    return PostgresAfterSalesRepository(DATABASE_URL)


def _expire(thread):
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE workflow_threads SET lease_until=now()-interval '1 second'"
            " WHERE thread_id=:t"), {"t": thread})
    engine.dispose()


def _owner_of(thread):
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        v = conn.execute(text(
            "SELECT lease_owner FROM workflow_threads WHERE thread_id=:t"),
            {"t": thread}).scalar()
    engine.dispose()
    return v


def _make_runner(svc, cp_path, lease_repo, owner):
    from src.agents import WorkflowRunner
    from src.agents.checkpoint import open_sqlite_checkpointer
    return WorkflowRunner(svc, checkpointer=open_sqlite_checkpointer(cp_path),
                          lease_repo=lease_repo, owner_id=owner, lease_duration_s=60)


def test_competition_single_progress_and_loser_zero_side_effects(repo, tmp_path):
    """两 Runner 竞争 resume 同 thread：持约方 A 推进；失约方 B 抛 ThreadLeaseError 且零副作用。"""
    from src.agents import WorkflowRunner
    from src.agents.checkpoint import close_sqlite_checkpointer
    from src.agents.runner import ThreadLeaseError
    from tests.unit.domain.after_sales.helpers import baseline_service

    svc = baseline_service()
    cp = tmp_path / "c1.sqlite"
    runner_a = _make_runner(svc, str(cp), repo, "owner-a")
    runner_b = _make_runner(svc, str(cp), repo, "owner-b")

    pending = runner_a.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="race")
    assert pending.waiting_approval
    assert _owner_of("race") == "owner-a"           # start 持约（workflow_threads 事实）
    op_id = pending.state["operation_id"]
    audit_before = len(svc.audit_log())

    # B（异 owner 未过期）resume → 失约稳定拒绝
    with pytest.raises(ThreadLeaseError):
        runner_b.resume("race", tenant_id="T1")
    # 零业务副作用：审计不增、操作仍等待审批
    assert len(svc.audit_log()) == audit_before
    assert svc.get_operation(op_id).status.value == "pending_approval"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")

    # A（持约）审批并 resume 推进 → 完成后 owner 释放
    runner_a.submit_decision(op_id, "approved")
    final = runner_a.resume("race", tenant_id="T1")
    assert final.finished and final.outcome == "refunded"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    assert _owner_of("race") is None                 # 完成后 owner 释放
    # 关连接以便临时目录清理
    close_sqlite_checkpointer(runner_a.graph.checkpointer)
    close_sqlite_checkpointer(runner_b.graph.checkpointer)


def test_expiry_takeover_and_old_owner_rejected_while_held(repo, tmp_path):
    """租约过期 → 新 owner（同 fingerprint）可接管；旧 owner 在他人持约期间再 resume 稳定拒绝。"""
    from src.agents.runner import ThreadLeaseError
    from tests.unit.domain.after_sales.helpers import baseline_service

    svc = baseline_service()
    cp = tmp_path / "c2.sqlite"
    runner_c = _make_runner(svc, str(cp), repo, "owner-c")
    runner_d = _make_runner(svc, str(cp), repo, "owner-d")

    pending = runner_c.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="takeover")
    assert pending.waiting_approval and _owner_of("takeover") == "owner-c"
    op_id = pending.state["operation_id"]

    _expire("takeover")                             # 租约过期
    # D 以同一 fingerprint 接管（fp 来自 workflow_threads 既有事实，杜绝覆盖）
    fp_d = repo.get_thread("T1", "takeover")[0]
    assert repo.claim_thread("T1", "takeover", "owner-d", 60, fp_d) is True
    assert _owner_of("takeover") == "owner-d"
    # D 持约期间 C（旧 owner）resume → 稳定拒绝（ThreadLeaseError），零推进
    with pytest.raises(ThreadLeaseError):
        runner_c.resume("takeover", tenant_id="T1")
    assert svc.get_operation(op_id).status.value == "pending_approval"   # C 未推进
    # D 审批并 resume 完成
    runner_d.submit_decision(op_id, "approved")
    final = runner_d.resume("takeover", tenant_id="T1")
    assert final.finished and svc.refunded_amount("ORD-1") == Decimal("100.00")
    assert _owner_of("takeover") is None
