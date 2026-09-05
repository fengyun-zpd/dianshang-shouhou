"""K6 e2e：经 FastAPI 的完整售后闭环（用户请求 → 审批 → 执行 → 审计）。"""
from decimal import Decimal

from fastapi.testclient import TestClient

from src.api import ApiIdentity, ApiTokenRegistry, create_app
from src.domain.after_sales import Role
from src.domain.after_sales.adapters import MemoryAdapter
from tests.unit.domain.after_sales.helpers import service_with_policies

POL = (("P-DAMAGED-FULL", ("damaged",), "1.00", 30),)


def _client():
    svc = service_with_policies(*POL)
    reg = ApiTokenRegistry()
    reg.register("t-a", ApiIdentity("agent", "T1", Role.AGENT))
    reg.register("t-ap", ApiIdentity("approver", "T1", Role.APPROVER))
    reg.register("t-s", ApiIdentity("system", "T1", Role.SYSTEM))
    return TestClient(create_app(MemoryAdapter(svc), reg)), svc


def _h(tok):
    return {"X-Api-Key": tok}


def test_e2e_full_refund_closed_loop():
    client, svc = _client()
    ticket = client.post("/api/tickets", json={
        "order_id": "ORD-1", "customer_id": "C1", "request_type": "refund",
        "reason": "商品破损", "reason_tags": ["damaged"], "idempotency_key": "e2e-tk",
    }, headers=_h("t-a"))
    assert ticket.status_code == 201
    tid = ticket.json()["ticket_id"]

    draft = client.post(f"/api/tickets/{tid}/refund-drafts", json={
        "amount": "100.00", "reason_detail": "破损", "idempotency_key": "e2e-d",
    }, headers=_h("t-a"))
    op_id = draft.json()["operation_id"]

    client.post(f"/api/operations/{op_id}/submit", json={}, headers=_h("t-a"))
    client.post(f"/api/operations/{op_id}/approve", json={}, headers=_h("t-ap"))
    ex = client.post(f"/api/operations/{op_id}/execute",
                     json={"external_result": "success"}, headers=_h("t-s"))
    assert ex.json()["status"] == "executed"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")

    audit = client.get(f"/api/audit?entity_type=operation&entity_id={op_id}",
                       headers=_h("t-a")).json()
    actions = [e["action"] for e in audit]
    for expected in ("create_refund", "submit", "approve", "execute"):
        assert expected in actions


def test_e2e_clarify_then_escalate_paths():
    client, svc = _client()
    # 缺少订单号会怎样：走 HTTP 时 body 必须含 order；转人工场景（无政策少件）→ 创建即 400 政策
    bad = client.post("/api/tickets", json={
        "order_id": "ORD-1", "customer_id": "C1", "request_type": "refund",
        "reason": "少件", "reason_tags": ["missing_item"], "idempotency_key": "e2e-np",
    }, headers=_h("t-a"))
    assert bad.status_code == 400
    assert bad.json()["code"] == "AFTER_SALES_POLICY_NOT_FOUND"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
