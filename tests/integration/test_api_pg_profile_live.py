"""PG profile API 端到端（阶段四第 3 节验收）：真实 HTTP 路径使用 PgCommandAdapter
（完整 AfterSalesApplicationPort：PgCommandService 命令 + Repository 行事实）。

前置：opspilot-pg（5433）或 DATABASE_URL；不可达整模块 skip。
覆盖端点：POST /api/tickets → refund-drafts → submit → approve → execute；
SQL 断言 refund_operations 状态/版本、approval_decisions 事实行、审计。
"""
import os
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from src.api import ApiIdentity, ApiTokenRegistry, create_app
from src.api.runtime import require_postgres_ready
from src.domain.after_sales import Role
from src.domain.after_sales.adapters import PgCommandAdapter
from src.domain.after_sales.pg_commands import PgCommandService
from src.repo import OrderRow, PolicyRow, PostgresAfterSalesRepository

ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot",
)
_SCHEMA = (ROOT / "src" / "repo" / "schema.sql").read_text(encoding="utf-8")


def _pg_available() -> bool:
    try:
        require_postgres_ready(DATABASE_URL)
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _pg_available(),
                                reason="PostgreSQL 不可达/版本不符：数据库集成未实测")


@pytest.fixture
def client():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        for table in ("workflow_threads", "audit_events", "idempotency_records",
                      "approval_decisions", "refund_operations", "tickets", "orders",
                      "policies", "order_items", "entity_seq"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn.execute(text(_SCHEMA))
    engine.dispose()
    repo = PostgresAfterSalesRepository(DATABASE_URL)
    repo.insert_order(OrderRow("T1", "ORD-1", "C1", "delivered", Decimal("100.00"), 2))
    repo.insert_policy(PolicyRow("T1", "P-1", "refund", '["damaged"]', 30,
                                 Decimal("1.0000"), "2020-01-01", 1))
    backend = PgCommandAdapter(PgCommandService(repo), repo)
    reg = ApiTokenRegistry()
    reg.register("tok-agent", ApiIdentity("agent-1", "T1", Role.AGENT))
    reg.register("tok-approver", ApiIdentity("approver-1", "T1", Role.APPROVER))
    reg.register("tok-system", ApiIdentity("system-1", "T1", Role.SYSTEM))
    return TestClient(create_app(backend, reg))


def _h(token, rid=None):
    h = {"X-Api-Key": token}
    if rid:
        h["X-Request-ID"] = rid
    return h


def test_pg_profile_http_full_flow_uses_pg_commands(client):
    """HTTP 全链：建单→草稿→提交→审批→执行；SQL 断言业务/审批事实/审计来自 PG。"""
    r = client.post("/api/tickets", json={
        "order_id": "ORD-1", "customer_id": "C1", "request_type": "refund",
        "reason": "商品破损", "reason_tags": ["damaged"], "idempotency_key": "tk-e2e",
    }, headers=_h("tok-agent"))
    assert r.status_code == 201, r.text
    ticket_id = r.json()["ticket_id"]
    d = client.post(f"/api/tickets/{ticket_id}/refund-drafts", json={
        "amount": "60.00", "reason_detail": "破损", "idempotency_key": "k-e2e",
    }, headers=_h("tok-agent"))
    assert d.status_code == 201, d.text
    op_id = d.json()["operation_id"]
    assert client.post(f"/api/operations/{op_id}/submit", json={},
                       headers=_h("tok-agent")).status_code == 200
    ap = client.post(f"/api/operations/{op_id}/approve", json={},
                     headers=_h("tok-approver"))
    assert ap.status_code == 200, ap.text
    ex = client.post(f"/api/operations/{op_id}/execute",
                     json={"external_result": "success"}, headers=_h("tok-system"))
    assert ex.status_code == 200, ex.text
    assert ex.json()["status"] == "executed"

    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        status = conn.execute(text(
            "SELECT status FROM refund_operations WHERE operation_id=:o"),
            {"o": op_id}).scalar()
        n_approval = conn.execute(text(
            "SELECT COUNT(*) FROM approval_decisions WHERE operation_id=:o"),
            {"o": op_id}).scalar()
        decided_by = conn.execute(text(
            "SELECT decided_by FROM approval_decisions WHERE operation_id=:o"),
            {"o": op_id}).scalar()
        n_audit = conn.execute(text(
            "SELECT COUNT(*) FROM audit_events WHERE action='execute'")).scalar()
        idem = conn.execute(text(
            "SELECT command_type, raw_key FROM idempotency_records WHERE idem_key=:k"),
            {"k": "T1:create_refund:k-e2e"}).fetchone()
    engine.dispose()
    assert status == "executed"
    assert n_approval == 1 and decided_by == "approver-1"
    assert n_audit == 1
    assert idem[0] == "create_refund" and idem[1] == "k-e2e"


def test_pg_profile_repeat_approve_http_409_no_extra_fact(client):
    """HTTP 重复审批 → 409 且 approval_decisions 不增行（幂等/唯一事实）。"""
    r = client.post("/api/tickets", json={
        "order_id": "ORD-1", "customer_id": "C1", "request_type": "refund",
        "reason": "商品破损", "reason_tags": ["damaged"], "idempotency_key": "tk-rep",
    }, headers=_h("tok-agent"))
    ticket_id = r.json()["ticket_id"]
    d = client.post(f"/api/tickets/{ticket_id}/refund-drafts", json={
        "amount": "60.00", "reason_detail": "x", "idempotency_key": "k-rep",
    }, headers=_h("tok-agent"))
    op_id = d.json()["operation_id"]
    client.post(f"/api/operations/{op_id}/submit", json={}, headers=_h("tok-agent"))
    assert client.post(f"/api/operations/{op_id}/approve", json={},
                       headers=_h("tok-approver")).status_code == 200
    again = client.post(f"/api/operations/{op_id}/approve", json={},
                        headers=_h("tok-approver"))
    assert again.status_code == 409
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT COUNT(*) FROM approval_decisions WHERE operation_id=:o"),
            {"o": op_id}).scalar()
    engine.dispose()
    assert n == 1
