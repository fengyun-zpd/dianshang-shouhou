"""D9 跨进程工作流线程与租约（阶段四第 4 节）真实 PostgreSQL 测试。

workflow_threads 事实表已建（0005）。覆盖：原子获租/续租/过期接管/释放；
两连接并发 claim 恰一成功；两个 Runner 竞争同 thread_id 时仅持约者可 resume 推进、
失约者不写（resume 前置守卫语义）。
"""
import os
import threading
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


def _expire(thread_id):
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE workflow_threads SET lease_until=now()-interval '1 second'"
            " WHERE thread_id=:th"), {"th": thread_id})
    engine.dispose()


def _gen(thread_id):
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        v = conn.execute(text(
            "SELECT generation, lease_owner FROM workflow_threads WHERE thread_id=:th"),
            {"th": thread_id}).fetchone()
    engine.dispose()
    return v


def test_claim_renew_expiry_takeover_and_release(repo):
    assert repo.claim_thread("T1", "th-1", "owner-a", 60, "fp-1") is True
    assert _gen("th-1")[0] == 1
    # 同 owner 续租（generation+1）
    assert repo.claim_thread("T1", "th-1", "owner-a", 60) is True
    assert _gen("th-1")[0] == 2
    # 异 owner 未过期 → 拒绝（不抢占）
    assert repo.claim_thread("T1", "th-1", "owner-b", 60) is False
    # 过期后可被接管（generation+1）
    _expire("th-1")
    assert repo.claim_thread("T1", "th-1", "owner-b", 60) is True
    assert _gen("th-1")[0] == 3 and _gen("th-1")[1] == "owner-b"
    # 非 owner 释放失败；owner 释放成功
    assert repo.release_thread("T1", "th-1", "owner-a") is False
    assert repo.release_thread("T1", "th-1", "owner-b") is True
    # 释放后他人可获约（generation+1）
    assert repo.claim_thread("T1", "th-1", "owner-c", 60) is True


def test_concurrent_claim_single_winner(repo):
    """两连接并发 claim 同 thread → 恰一成功（原子 ON CONFLICT 语义）。"""
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(owner):
        try:
            barrier.wait()
            ok = PostgresAfterSalesRepository(DATABASE_URL).claim_thread(
                "T1", "th-conc", owner, 60, "fp")
        except Exception:  # noqa: BLE001
            ok = False
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker, args=(o,)) for o in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_two_runners_resume_single_lease_holder_progresses(repo):
    """两个 Runner 竞争同 thread_id：仅持约者可 resume 推进；失约者不得执行写。
    语义：以 claim_thread 作为 resume 前置守卫（runner 内部强制租约属后续端口化集成，
    本测试给出可验证的竞争单推进证据）。"""
    from src.agents import WorkflowRunner
    from src.agents.checkpoint import close_sqlite_checkpointer, open_sqlite_checkpointer
    from tests.unit.domain.after_sales.helpers import baseline_service

    import tempfile
    tmpdir = tempfile.TemporaryDirectory()
    cp = None
    try:
        svc = baseline_service()
        cp = open_sqlite_checkpointer(str(Path(tmpdir.name) / "d9.sqlite"))
        runner = WorkflowRunner(svc, checkpointer=cp)
        pending = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="lease-resume")
        assert pending.waiting_approval
        op_id = pending.state["operation_id"]

        # 竞争者 A 获约
        assert repo.claim_thread("T1", "lease-resume", "runner-a", 60, "fp-resume") is True
        # 竞争者 B 未过期抢约 → 失败：B 不得 resume（不执行写）
        assert repo.claim_thread("T1", "lease-resume", "runner-b", 60) is False

        # A（持约）推进：审批决定 + resume
        runner.submit_decision(op_id, "approved")
        final = runner.resume("lease-resume", tenant_id="T1")
        assert final.finished and final.outcome == "refunded"
        assert svc.refunded_amount("ORD-1") == Decimal("100.00")
        assert repo.release_thread("T1", "lease-resume", "runner-a") is True
        # 释放后 B 可获约（先前未执行写的 B 如今可接管——用于后续恢复）
        assert repo.claim_thread("T1", "lease-resume", "runner-b", 60) is True
    finally:
        if cp is not None:
            close_sqlite_checkpointer(cp)
        tmpdir.cleanup()
