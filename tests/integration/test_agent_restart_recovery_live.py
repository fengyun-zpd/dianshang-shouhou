"""进程重启恢复（PG profile，live）：线程绑定不依赖内存字典。

场景：
- 实例 A 启动工作流并停在审批中断；
- 关闭实例 A（关闭 checkpointer，模拟进程退出，**不释放租约**）；
- 租约过期后实例 B 用**同一 PostgreSQL + 同一固定 checkpoint** 接管：
  能按 (tenant_id, thread_id) 读到 state、按领域事实恢复 decision 并继续执行；
- 错误租户读取 → 统一 404（不暴露所属租户）；
- 同名线程在不同租户下互不冲突；
- 租约语义仍有效：他人持约未过期时拒绝（ThreadLeaseError → 409）。

破坏性 PG 集成只允许 `OPSPILOT_TEST_DATABASE_URL`（opspilot_test_*@localhost）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.pg_live import live_test_db_url, pg_reachable, reset_test_schema

TEST_DB_URL = live_test_db_url()

pytestmark = pytest.mark.skipif(
    TEST_DB_URL is None or not pg_reachable(TEST_DB_URL),
    reason="OPSPILOT_TEST_DATABASE_URL 未设置或 PostgreSQL 不可达："
           "未使用隔离测试库，跳过破坏性集成（PG 集成未实测）")

REQUEST = "订单 ORD-1 商品破损，要求退款"
H_AGENT = {"X-Api-Key": "demo-agent"}
H_APPROVER = {"X-Api-Key": "demo-approver"}
H_AGENT_T2 = {"X-Api-Key": "demo-agent-t2"}
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fresh_db():
    reset_test_schema(TEST_DB_URL)


def _seed_orders() -> None:
    """两个租户各一条等价订单与政策（T2 用于同名线程/跨租户场景）。"""
    from src.repo import OrderRow, PolicyRow, PostgresAfterSalesRepository
    repo = PostgresAfterSalesRepository(TEST_DB_URL)
    repo.insert_order(OrderRow("T1", "ORD-1", "C1", "delivered", Decimal("100.00"), 2))
    repo.insert_policy(PolicyRow("T1", "P-1", "refund", '["damaged"]', 30,
                                 Decimal("1.0000"), "1970-01-01", 1))
    repo.insert_order(OrderRow("T2", "ORD-2", "C9", "delivered", Decimal("100.00"), 2))
    repo.insert_policy(PolicyRow("T2", "P-2", "refund", '["damaged"]', 30,
                                 Decimal("1.0000"), "1970-01-01", 1))


def _executed_amount(tenant: str) -> Decimal:
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        total = conn.execute(text(
            "SELECT COALESCE(SUM(amount), 0) FROM refund_operations"
            " WHERE tenant_id=:t AND status='executed'"), {"t": tenant}).scalar()
    engine.dispose()
    return Decimal(total)


def _instance(checkpoint_path, owner_id: str, lease_duration_s: int = 60) -> dict:
    from src.api import create_app
    from scripts.run_api import build_pg_backend, demo_token_registry
    built = build_pg_backend(TEST_DB_URL, checkpoint_path=str(checkpoint_path),
                             owner_id=owner_id, lease_duration_s=lease_duration_s)
    built["app"] = create_app(built["backend"], demo_token_registry(),
                              pg_probe=built["probe"], require_expected_version=True,
                              agent_runner=built["runner"])
    return built


def _close(built: dict) -> None:
    from src.agents.checkpoint import close_sqlite_checkpointer
    close_sqlite_checkpointer(built["checkpointer"])


def _wait_for_lease_expired(thread_id: str, timeout_s: float = 8.0) -> None:
    """Use PostgreSQL time as the clock instead of a fixed wall-clock sleep."""
    engine = create_engine(TEST_DB_URL)
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            with engine.connect() as conn:
                expired = conn.execute(text(
                    "SELECT lease_until IS NULL OR lease_until <= now()"
                    " FROM workflow_threads WHERE tenant_id='T1' AND thread_id=:th"
                ), {"th": thread_id}).scalar()
            if expired:
                return
            time.sleep(0.05)
    finally:
        engine.dispose()
    raise AssertionError(f"workflow_threads lease did not expire within {timeout_s}s")


def _worker(mode: str, checkpoint: Path, owner: str, thread_id: str,
            lease_duration_s: int = 60) -> dict:
    cmd = [
        sys.executable, str(ROOT / "scripts" / "agent_restart_worker.py"),
        "--mode", mode, "--url", TEST_DB_URL, "--checkpoint", str(checkpoint),
        "--owner", owner, "--thread", thread_id,
        "--lease-duration", str(lease_duration_s),
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(cmd, cwd=str(ROOT), env=env,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", check=False)
    assert completed.returncode == 0, (
        f"worker {mode} failed: stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, f"worker {mode} produced no JSON output"
    return json.loads(lines[-1])


def test_restart_recovers_thread_and_continues(fresh_db, tmp_path):
    """独立进程 A 中断 → 退出 → 独立进程 B 接管并继续执行。"""
    _seed_orders()
    ckpt = tmp_path / "agent.sqlite"

    started = _worker("start", ckpt, "owner-A", "restart-thread", lease_duration_s=1)
    assert started["status_code"] == 200, started
    assert started["body"]["waiting_approval"] is True
    assert started["body"]["state"]["tenant_id"] == "T1"
    assert _executed_amount("T1") == Decimal("0.00")

    # Worker A exits without releasing its lease, then B waits on DB time.
    _wait_for_lease_expired("restart-thread")
    finished = _worker("finish", ckpt, "owner-B", "restart-thread")
    assert finished["state_status"] == 200
    assert finished["state"]["waiting_approval"] is True
    assert finished["approve_status"] == 200, finished
    assert finished["decision_status"] == 200, finished
    assert finished["decision"]["outcome"] == "refunded"
    assert _executed_amount("T1") == Decimal("100.00")


def test_wrong_tenant_cannot_read_recovered_thread(fresh_db, tmp_path):
    """错误租户读同一线程 → 统一 404，且响应不含所属租户。"""
    _seed_orders()
    ckpt = tmp_path / "tenant.sqlite"
    a = _instance(ckpt, "owner-t1")
    try:
        with TestClient(a["app"]) as client_a:
            assert client_a.post("/api/v1/agent/start", json={
                "message": REQUEST, "thread_id": "tenant-thread", "order_id_hint": "ORD-1",
            }, headers=H_AGENT).status_code == 200
    finally:
        _close(a)

    b = _instance(ckpt, "owner-t2")
    try:
        with TestClient(b["app"]) as client_b:
            denied = client_b.get("/api/v1/agent/tenant-thread/state", headers=H_AGENT_T2)
            assert denied.status_code == 404, "跨租户必须是通用 404"
            assert denied.json()["code"] == "AGENT_THREAD_NOT_FOUND"
            assert "T1" not in denied.text, "不得泄露所属租户"
            # 本租户同线程名 = 另一个线程，可正常创建
            created = client_b.post("/api/v1/agent/start", json={
                "message": REQUEST.replace("ORD-1", "ORD-2"), "thread_id": "tenant-thread",
                "order_id_hint": "ORD-2",
            }, headers=H_AGENT_T2)
            assert created.status_code == 200, created.text
            assert created.json()["state"]["tenant_id"] == "T2"
            # 原租户线程仍完好
            own = client_b.get("/api/v1/agent/tenant-thread/state", headers=H_AGENT)
            assert own.status_code == 200 and own.json()["state"]["tenant_id"] == "T1"
    finally:
        _close(b)


def test_lease_semantics_still_enforced_between_instances(fresh_db, tmp_path):
    """租约仍有效：A 持约未过期时 B 拒绝推进（409 AGENT_THREAD_LEASE_HELD），零副作用。"""
    _seed_orders()
    ckpt = tmp_path / "lease.sqlite"
    a = _instance(ckpt, "owner-lease-A", lease_duration_s=600)
    b = _instance(ckpt, "owner-lease-B")
    try:
        with TestClient(a["app"]) as client_a, TestClient(b["app"]) as client_b:
            started = client_a.post("/api/v1/agent/start", json={
                "message": REQUEST, "thread_id": "lease-thread", "order_id_hint": "ORD-1",
            }, headers=H_AGENT)
            op_id = started.json()["operation_id"]

            # B 在读 state 前必须先获租约？——只读不推进图，允许读视图
            assert client_b.get("/api/v1/agent/lease-thread/state",
                                headers=H_AGENT).status_code == 200

            # 但 B 不能推进图：A 持约未过期 → 409
            blocked = client_b.post("/api/v1/agent/lease-thread/decision", json={},
                                    headers=H_APPROVER)
            assert blocked.status_code == 409, blocked.text
            assert blocked.json()["code"] == "AGENT_THREAD_LEASE_HELD"
            assert _executed_amount("T1") == Decimal("0.00")

            # A 自己可以继续（持约方）
            version = a["backend"].get_operation("T1", op_id).version
            client_a.post(f"/api/operations/{op_id}/approve",
                          json={"expected_version": version}, headers=H_APPROVER)
            done = client_a.post("/api/v1/agent/lease-thread/decision", json={},
                                 headers=H_APPROVER)
            assert done.status_code == 200 and done.json()["outcome"] == "refunded"
            assert _executed_amount("T1") == Decimal("100.00")
    finally:
        _close(a)
        _close(b)
