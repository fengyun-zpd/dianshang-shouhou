"""D9 跨进程工作流线程与租约（阶段四第 4 节）真实 PostgreSQL 测试。

workflow_threads 事实表已建（0005）。覆盖：原子获租/续租/过期接管/释放；
两连接并发 claim 恰一成功；两个 Runner 竞争同 thread_id 时仅持约者可 resume 推进、
失约者不写（resume 前置守卫语义）。
"""
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.pg_live import live_test_db_url, pg_reachable, reset_test_schema

from src.repo import PostgresAfterSalesRepository

TEST_DB_URL = live_test_db_url()

pytestmark = pytest.mark.skipif(
    TEST_DB_URL is None or not pg_reachable(TEST_DB_URL),
    reason="OPSPILOT_TEST_DATABASE_URL 未设置或 PostgreSQL 不可达："
           "未使用隔离测试库，跳过破坏性集成（PG 集成未实测）")


@pytest.fixture
def repo():
    """guard 通过后重建全部业务表（opspilot_test_* 隔离库），返回 PG Repository。"""
    reset_test_schema(TEST_DB_URL)
    return PostgresAfterSalesRepository(TEST_DB_URL)


def _expire(thread_id):
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE workflow_threads SET lease_until=now()-interval '1 second'"
            " WHERE thread_id=:th"), {"th": thread_id})
    engine.dispose()


def _gen(thread_id):
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        v = conn.execute(text(
            "SELECT generation, lease_owner FROM workflow_threads WHERE thread_id=:th"),
            {"th": thread_id}).fetchone()
    engine.dispose()
    return v


def _fp(thread_id):
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        v = conn.execute(text(
            "SELECT request_fingerprint FROM workflow_threads WHERE thread_id=:th"),
            {"th": thread_id}).scalar()
    engine.dispose()
    return v


def test_claim_renew_expiry_takeover_and_release(repo):
    assert repo.claim_thread("T1", "th-1", "owner-a", 60, "fp-1") is True
    assert _gen("th-1")[0] == 1
    # 同 owner 续租（同 fp，generation+1；fingerprint 不可变）
    assert repo.claim_thread("T1", "th-1", "owner-a", 60, "fp-1") is True
    assert _gen("th-1")[0] == 2
    assert _fp("th-1") == "fp-1"
    # 同 owner 异 fp → 拒绝（R3：fingerprint 不可变；重复 start 异请求不覆盖事实）
    assert repo.claim_thread("T1", "th-1", "owner-a", 60, "fp-other") is False
    assert _fp("th-1") == "fp-1"
    # 异 owner 同 fp 未过期 → 拒绝（不抢占）
    assert repo.claim_thread("T1", "th-1", "owner-b", 60, "fp-1") is False
    # 过期后：同 fp 可被接管（generation+1）；异 fp 接管仍拒绝（R4）
    _expire("th-1")
    assert repo.claim_thread("T1", "th-1", "owner-b", 60, "fp-other") is False
    assert repo.claim_thread("T1", "th-1", "owner-b", 60, "fp-1") is True
    assert _gen("th-1")[0] == 3 and _gen("th-1")[1] == "owner-b"
    assert _fp("th-1") == "fp-1"
    # 非 owner 释放失败；owner 释放成功
    assert repo.release_thread("T1", "th-1", "owner-a") is False
    assert repo.release_thread("T1", "th-1", "owner-b") is True
    # 释放后他人可获约（同 fp，generation+1）
    assert repo.claim_thread("T1", "th-1", "owner-c", 60, "fp-1") is True


def test_concurrent_claim_single_winner(repo):
    """两连接并发 claim 同 thread → 恰一成功（原子 ON CONFLICT 语义）。"""
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(owner):
        try:
            barrier.wait()
            ok = PostgresAfterSalesRepository(TEST_DB_URL).claim_thread(
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
    from src.domain.after_sales.adapters import MemoryAdapter
    from tests.unit.domain.after_sales.helpers import baseline_service

    import tempfile
    tmpdir = tempfile.TemporaryDirectory()
    cp = None
    try:
        svc = baseline_service()
        cp = open_sqlite_checkpointer(str(Path(tmpdir.name) / "d9.sqlite"))
        runner = WorkflowRunner(MemoryAdapter(svc), checkpointer=cp)
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
        # 释放后 B 可获约（同 fp 接管；先前未执行写的 B 如今可恢复）
        assert repo.claim_thread("T1", "lease-resume", "runner-b", 60, "fp-resume") is True
    finally:
        if cp is not None:
            close_sqlite_checkpointer(cp)
        tmpdir.cleanup()
