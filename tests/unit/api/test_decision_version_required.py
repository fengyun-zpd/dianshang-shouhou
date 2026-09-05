"""R6：pg profile 审批 expected_version 必填（服务端不代填）测试。"""
from decimal import Decimal

from fastapi.testclient import TestClient

from src.api import ApiIdentity, ApiTokenRegistry, create_app
from src.domain.after_sales import Role
from tests.unit.domain.after_sales.helpers import service_with_policies


def _client(require_expected_version: bool):
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))
    reg = ApiTokenRegistry()
    reg.register("tok-agent", ApiIdentity("agent-1", "T1", Role.AGENT))
    reg.register("tok-approver", ApiIdentity("approver-1", "T1", Role.APPROVER))
    return TestClient(create_app(svc, reg, require_expected_version=require_expected_version))


def _pending_operation(client):
    r = client.post("/api/tickets", json={
        "order_id": "ORD-1", "customer_id": "C1", "request_type": "refund",
        "reason": "商品破损", "reason_tags": ["damaged"], "idempotency_key": "tk-r6",
    }, headers={"X-Api-Key": "tok-agent"})
    ticket_id = r.json()["ticket_id"]
    d = client.post(f"/api/tickets/{ticket_id}/refund-drafts", json={
        "amount": "60.00", "reason_detail": "x", "idempotency_key": "k-r6",
    }, headers={"X-Api-Key": "tok-agent"})
    op_id = d.json()["operation_id"]
    client.post(f"/api/operations/{op_id}/submit", json={},
                headers={"X-Api-Key": "tok-agent"})
    return op_id


def test_require_expected_version_missing_returns_422():
    """pg profile：approve/reject 缺 expected_version → 422 稳定错误（服务端不代填）。"""
    client = _client(require_expected_version=True)
    op_id = _pending_operation(client)
    for path in (f"/api/operations/{op_id}/approve", f"/api/operations/{op_id}/reject"):
        resp = client.post(path, json={}, headers={"X-Api-Key": "tok-approver"})
        assert resp.status_code == 422, (path, resp.text)
        assert "EXPECTED_VERSION_REQUIRED" in resp.text


def test_require_expected_version_present_ok():
    """pg profile：带 expected_version 审批正常。"""
    client = _client(require_expected_version=True)
    op_id = _pending_operation(client)
    r = client.post(f"/api/operations/{op_id}/approve", json={"expected_version": 1},
                    headers={"X-Api-Key": "tok-approver"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"


def test_default_profile_legacy_lenient():
    """默认（memory/演示）保持宽松：缺 expected_version 服务端兜底（向后兼容）。"""
    client = _client(require_expected_version=False)
    op_id = _pending_operation(client)
    r = client.post(f"/api/operations/{op_id}/approve", json={},
                    headers={"X-Api-Key": "tok-approver"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
