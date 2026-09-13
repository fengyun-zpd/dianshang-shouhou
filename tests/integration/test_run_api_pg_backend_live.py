"""R7/PA3 轻量冒烟：run_api --backend pg 装配链真实可构建
（隔离测试库 OPSPILOT_TEST_DATABASE_URL 可达时；绝不回退 DATABASE_URL 主库）。

- build_pg_backend(url, ...) 产出：PgCommandAdapter（完整 Port）＋ SQLite 持久
  checkpointer ＋ WorkflowRunner(lease_repo=repo, owner_id=稳定)；
- create_app 后 TestClient /health/live 200、/health/ready 200（PG 就绪）；
- 装配不 seed 任何 PG 业务数据（orders/tickets 均空——防污染未知库）。
"""
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.pg_live import live_test_db_url, pg_reachable, reset_test_schema

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DB_URL = live_test_db_url()

pytestmark = pytest.mark.skipif(
    TEST_DB_URL is None or not pg_reachable(TEST_DB_URL),
    reason="OPSPILOT_TEST_DATABASE_URL 未设置或 PostgreSQL 不可达："
           "未使用隔离测试库，跳过破坏性集成（PG 集成未实测）")


@pytest.fixture
def fresh_db():
    """guard 通过后重建全部业务表（opspilot_test_* 隔离库）。"""
    reset_test_schema(TEST_DB_URL)


def _close_cp(built) -> None:
    from src.agents.checkpoint import close_sqlite_checkpointer
    close_sqlite_checkpointer(built["checkpointer"])


def test_build_pg_backend_assembly_and_health(fresh_db, tmp_path):
    """装配链真实构建：Port adapter + 持久 checkpoint + WorkflowRunner(lease)。"""
    from scripts.run_api import build_pg_backend
    built = build_pg_backend(
        TEST_DB_URL,
        checkpoint_path=str(tmp_path / "ck.sqlite"),
        owner_id="live-test-owner",
    )
    try:
        assert built["owner_id"] == "live-test-owner"
        from src.domain.after_sales.adapters import PgCommandAdapter
        assert isinstance(built["backend"], PgCommandAdapter)
        assert built["runner"]._lease_repo is built["repo"]     # D9 租约强制
        assert Path(built["checkpoint_path"]).exists()

        from src.api import ApiTokenRegistry, create_app
        app = create_app(built["backend"], ApiTokenRegistry(),
                         pg_probe=built["probe"])
        with TestClient(app) as client:
            assert client.get("/health/live").status_code == 200
            assert client.get("/health/ready").status_code == 200   # PG 就绪反映
    finally:
        _close_cp(built)


def test_build_pg_backend_never_seeds_business_data(fresh_db, tmp_path):
    """pg 装配不自动 seed 业务数据（订单/工单表为空）。"""
    from scripts.run_api import build_pg_backend
    built = build_pg_backend(
        TEST_DB_URL,
        checkpoint_path=str(tmp_path / "ck2.sqlite"),
        owner_id="live-test-owner-2",
    )
    try:
        engine = create_engine(TEST_DB_URL)
        with engine.connect() as conn:
            orders = conn.execute(text("SELECT COUNT(*) FROM orders")).scalar()
            tickets = conn.execute(text("SELECT COUNT(*) FROM tickets")).scalar()
            ops = conn.execute(text("SELECT COUNT(*) FROM refund_operations")).scalar()
        engine.dispose()
        assert (orders, tickets, ops) == (0, 0, 0)
    finally:
        _close_cp(built)


# ---------- PG profile 的 Agent HTTP 主链路（真实 PostgreSQL 事实源） ----------

def _seed_pg_order() -> None:
    """隔离库 seed 等价事实：T1 / ORD-1 实付 100.00 + 破损全额政策。"""
    from src.repo import PolicyRow, PostgresAfterSalesRepository, OrderRow
    repo = PostgresAfterSalesRepository(TEST_DB_URL)
    repo.insert_order(OrderRow("T1", "ORD-1", "C1", "delivered", Decimal("100.00"), 2))
    repo.insert_policy(PolicyRow("T1", "P-1", "refund", '["damaged"]', 30,
                                 Decimal("1.0000"), "1970-01-01", 1))


def _executed_amount():
    """PostgreSQL 事实：该订单已执行退款金额合计（Decimal）。"""
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        total = conn.execute(text(
            "SELECT COALESCE(SUM(amount), 0) FROM refund_operations "
            "WHERE tenant_id='T1' AND status='executed'")).scalar()
    engine.dispose()
    return Decimal(total)


def test_pg_profile_agent_http_uses_real_pg_runner(fresh_db, tmp_path):
    """PG profile：/api/v1/agent/* 由真实 PG runner 驱动；不注册 memory reset 路由。

    完整走一遍 start → 领域审批事实 → decision(空 body) → state，
    并以 PostgreSQL 行（refund_operations.status='executed' 金额）作为最终事实验收。
    """
    from src.domain.after_sales.adapters import PgCommandAdapter
    from src.api import create_app
    from scripts.run_api import build_pg_backend, demo_token_registry

    _seed_pg_order()
    built = build_pg_backend(TEST_DB_URL, checkpoint_path=str(tmp_path / "ck3.sqlite"),
                             owner_id="live-test-owner-3")
    try:
        runner = built["runner"]
        assert isinstance(runner.backend, PgCommandAdapter), "PG profile 必须使用真实 PG runner"
        assert runner._lease_repo is built["repo"], "PG profile 必须启用 D9 线程租约"

        app = create_app(built["backend"], demo_token_registry(), pg_probe=built["probe"],
                         require_expected_version=True, agent_runner=runner)
        with TestClient(app) as client:
            # 1) PG profile 不暴露 memory reset（路由未注册）
            assert client.post("/api/demo/reset",
                               headers={"X-Api-Key": "demo-agent"}).status_code == 404

            # 2) start → 进入审批等待（金额来自 PG 政策行）
            started = client.post("/api/v1/agent/start", json={
                "message": "订单 ORD-1 商品破损，要求退款", "thread_id": "pg-agent-thread",
                "order_id_hint": "ORD-1",
            }, headers={"X-Api-Key": "demo-agent"})
            assert started.status_code == 200, started.text
            body = started.json()
            op_id = body["operation_id"]
            assert body["waiting_approval"] is True
            assert body["state"]["action_draft"]["amount"] == "100.00"
            assert _executed_amount() == Decimal("0.00")

            # 3) 领域审批事实写入 PG（pg profile 要求客户端提交 expected_version）
            version = built["backend"].get_operation("T1", op_id).version
            approved = client.post(f"/api/operations/{op_id}/approve",
                                   json={"expected_version": version},
                                   headers={"X-Api-Key": "demo-approver"})
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "approved"

            # 4) decision 空 body → apply_decision 重读 PG 事实 → 执行
            decided = client.post("/api/v1/agent/pg-agent-thread/decision", json={},
                                  headers={"X-Api-Key": "demo-approver"})
            assert decided.status_code == 200, decided.text
            assert decided.json()["outcome"] == "refunded"
            assert _executed_amount() == Decimal("100.00")

            # 5) state 只读视图（租户作用域）
            view = client.get("/api/v1/agent/pg-agent-thread/state",
                              headers={"X-Api-Key": "demo-agent"})
            assert view.status_code == 200
            assert view.json()["outcome"] == "refunded"
            assert view.json()["operation_id"] == op_id
    finally:
        _close_cp(built)


def test_pg_profile_agent_rejects_forged_decision_body(fresh_db, tmp_path):
    """PG profile：decision 携带审批结论字段 → 422；领域事实不变更、无执行。"""
    from src.api import create_app
    from scripts.run_api import build_pg_backend, demo_token_registry

    _seed_pg_order()
    built = build_pg_backend(TEST_DB_URL, checkpoint_path=str(tmp_path / "ck4.sqlite"),
                             owner_id="live-test-owner-4")
    try:
        app = create_app(built["backend"], demo_token_registry(), pg_probe=built["probe"],
                         require_expected_version=True, agent_runner=built["runner"])
        with TestClient(app) as client:
            started = client.post("/api/v1/agent/start", json={
                "message": "订单 ORD-1 商品破损，要求退款", "thread_id": "pg-forge-thread",
                "order_id_hint": "ORD-1",
            }, headers={"X-Api-Key": "demo-agent"}).json()
            r = client.post("/api/v1/agent/pg-forge-thread/decision",
                            json={"decision": "approved"},
                            headers={"X-Api-Key": "demo-approver"})
            assert r.status_code == 422, "decision 接口不得携带审批结论字段"
            assert _executed_amount() == Decimal("0.00")
            st = client.get("/api/v1/agent/pg-forge-thread/state",
                            headers={"X-Api-Key": "demo-agent"}).json()
            assert st["waiting_approval"] is True
            assert started["operation_id"]
    finally:
        _close_cp(built)
