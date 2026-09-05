"""K5：FastAPI 接入层测试（认证/租户/越权/幂等/非法状态/unknown/审计）。"""
from decimal import Decimal

from fastapi.testclient import TestClient

from src.api import ApiIdentity, ApiTokenRegistry, create_app
from src.domain.after_sales import Order, OrderItem, OrderStatus, PolicyRule, RequestType, Role
from tests.unit.domain.after_sales.helpers import service_with_policies

POL = (("P-DAMAGED-FULL", ("damaged",), "1.00", 30),)


def _make_client():
    svc = service_with_policies(*POL)
    svc.seed_order(Order(
        order_id="ORD-T2", tenant_id="T2", customer_id="C9",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("200.00"),
        items=[OrderItem(sku="S2", name="x", quantity=1, unit_price=Decimal("200.00"))],
        days_since_sign=1,
    ))
    reg = ApiTokenRegistry()
    reg.register("tok-agent-1", ApiIdentity("agent-1", "T1", Role.AGENT))
    reg.register("tok-approver-1", ApiIdentity("approver-1", "T1", Role.APPROVER))
    reg.register("tok-system-1", ApiIdentity("system-1", "T1", Role.SYSTEM))
    reg.register("tok-agent-2", ApiIdentity("agent-2", "T2", Role.AGENT))
    reg.register("tok-customer-c1", ApiIdentity("cust-1", "T1", Role.CUSTOMER, customer_id="C1"))
    return TestClient(create_app(svc, reg)), svc


def _h(token=None, rid=None):
    headers = {}
    if token:
        headers["X-Api-Key"] = token
    if rid:
        headers["X-Request-ID"] = rid
    return headers


def _open_ticket(client, token="tok-agent-1", key="tk-1", reason="商品破损",
                 tags=("damaged",), order="ORD-1"):
    return client.post("/api/tickets", json={
        "order_id": order, "customer_id": "C1", "request_type": "refund",
        "reason": reason, "reason_tags": list(tags), "idempotency_key": key,
    }, headers=_h(token))


# ---------- 认证 ----------

def test_unauthenticated_401():
    client, _ = _make_client()
    assert client.get("/api/tickets/TKT-1", headers=_h()).status_code == 401
    assert client.get("/api/tickets/TKT-1", headers=_h("bad-token")).status_code == 401


# ---------- 工单创建 / 幂等 ----------

def test_create_and_get_ticket_and_idempotency():
    client, _ = _make_client()
    r1 = _open_ticket(client, key="dup-tk")
    assert r1.status_code == 201
    t1 = r1.json()["ticket_id"]
    r2 = _open_ticket(client, key="dup-tk")          # 同键同载荷 → 原工单
    assert r2.status_code == 201 and r2.json()["ticket_id"] == t1
    r3 = _open_ticket(client, key="dup-tk", reason="少件", tags=("missing_item",))
    assert r3.status_code == 409                     # 同键异载荷
    assert r3.json()["code"] == "AFTER_SALES_IDEMPOTENCY_CONFLICT"
    assert "request_id" in r3.json()

    got = client.get(f"/api/tickets/{t1}", headers=_h("tok-agent-1"))
    assert got.status_code == 200 and got.json()["status"] == "open"


def test_cross_tenant_read_forbidden():
    client, _ = _make_client()
    t1 = _open_ticket(client, key="tk-xt").json()["ticket_id"]
    r = client.get(f"/api/tickets/{t1}", headers=_h("tok-agent-2"))  # T2 读 T1 工单
    assert r.status_code == 403
    assert r.json()["code"] == "AFTER_SALES_TENANT_MISMATCH"


def test_customer_only_own_ticket():
    client, _ = _make_client()
    ok = client.post("/api/tickets", json={
        "order_id": "ORD-1", "customer_id": "C1", "request_type": "refund",
        "reason": "商品破损", "reason_tags": ["damaged"], "idempotency_key": "tk-c",
    }, headers=_h("tok-customer-c1"))
    assert ok.status_code == 201
    bad = client.post("/api/tickets", json={
        "order_id": "ORD-1", "customer_id": "C2", "request_type": "refund",
        "reason": "商品破损", "reason_tags": ["damaged"], "idempotency_key": "tk-c2",
    }, headers=_h("tok-customer-c1"))
    assert bad.status_code == 403                          # 客户不能替他人建单


# ---------- 草稿 → 提交 → 审批 → 执行 ----------

def test_full_flow_agent_approver_system():
    client, svc = _make_client()
    t1 = _open_ticket(client, key="tk-flow").json()["ticket_id"]
    draft = client.post(f"/api/tickets/{t1}/refund-drafts", json={
        "amount": "60.00", "reason_detail": "破损", "idempotency_key": "k-d1",
    }, headers=_h("tok-agent-1"))
    assert draft.status_code == 201
    op_id = draft.json()["operation_id"]

    sub = client.post(f"/api/operations/{op_id}/submit", json={}, headers=_h("tok-agent-1"))
    assert sub.status_code == 200 and sub.json()["status"] == "pending_approval"

    # Agent 越权审批 → 403（错误码不被改写）
    denied = client.post(f"/api/operations/{op_id}/approve", json={}, headers=_h("tok-agent-1"))
    assert denied.status_code == 403
    assert denied.json()["code"] == "AFTER_SALES_PERMISSION_DENIED"

    ap = client.post(f"/api/operations/{op_id}/approve", json={}, headers=_h("tok-approver-1"))
    assert ap.status_code == 200 and ap.json()["status"] == "approved"

    ex = client.post(f"/api/operations/{op_id}/execute",
                     json={"external_result": "success"}, headers=_h("tok-system-1"))
    assert ex.status_code == 200 and ex.json()["status"] == "executed"
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")


def test_approve_without_submit_conflict():
    client, _ = _make_client()
    t1 = _open_ticket(client, key="tk-ns").json()["ticket_id"]
    draft = client.post(f"/api/tickets/{t1}/refund-drafts", json={
        "amount": "10.00", "reason_detail": "x", "idempotency_key": "k-ns",
    }, headers=_h("tok-agent-1")).json()
    r = client.post(f"/api/operations/{draft['operation_id']}/approve", json={},
                    headers=_h("tok-approver-1"))
    assert r.status_code == 409
    assert r.json()["code"] == "AFTER_SALES_INVALID_STATE_TRANSITION"


# ---------- unknown / 对账 / 换键拒绝 ----------

def test_unknown_reconcile_and_no_new_key():
    client, svc = _make_client()
    t1 = _open_ticket(client, key="tk-uk").json()["ticket_id"]
    draft = client.post(f"/api/tickets/{t1}/refund-drafts", json={
        "amount": "60.00", "reason_detail": "x", "idempotency_key": "k-uk",
    }, headers=_h("tok-agent-1")).json()
    op_id = draft["operation_id"]
    client.post(f"/api/operations/{op_id}/submit", json={}, headers=_h("tok-agent-1"))
    client.post(f"/api/operations/{op_id}/approve", json={}, headers=_h("tok-approver-1"))
    ex = client.post(f"/api/operations/{op_id}/execute",
                     json={"external_result": "timeout"}, headers=_h("tok-system-1"))
    assert ex.status_code == 200 and ex.json()["status"] == "unknown"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")

    # unknown 存在：换新键创建草稿 → 拒绝
    newk = client.post(f"/api/tickets/{t1}/refund-drafts", json={
        "amount": "10.00", "reason_detail": "换键", "idempotency_key": "k-new",
    }, headers=_h("tok-agent-1"))
    assert newk.status_code == 409
    assert newk.json()["code"] == "AFTER_SALES_OPERATION_UNKNOWN_CONFLICT"

    # 原操作对账收口（system）
    rec = client.post(f"/api/operations/{op_id}/reconcile",
                      json={"result": "success"}, headers=_h("tok-system-1"))
    assert rec.status_code == 200 and rec.json()["status"] == "executed"
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")


# ---------- request_id / 审计 ----------

def test_request_id_and_audit_tenant_scoped():
    client, _ = _make_client()
    r = client.get("/api/tickets/TKT-NOPE", headers=_h("tok-agent-1", rid="req-abc"))
    assert r.status_code == 404
    assert r.json()["request_id"] == "req-abc"
    assert r.headers.get("X-Request-ID") == "req-abc"

    t1 = _open_ticket(client, key="tk-aud").json()["ticket_id"]
    audit = client.get(f"/api/audit?entity_type=ticket&entity_id={t1}",
                       headers=_h("tok-agent-1"))
    assert audit.status_code == 200
    assert any(e["action"] == "create_ticket" for e in audit.json())

    # T2 视角查不到 T1 工单审计（租户作用域）
    audit2 = client.get(f"/api/audit?entity_type=ticket&entity_id={t1}",
                        headers=_h("tok-agent-2"))
    assert audit2.status_code == 200 and audit2.json() == []


# ---------- 第三阶段安全审查补充 ----------

def test_body_tenant_id_is_ignored_identity_wins():
    """TenantContext 只来自认证：请求体携带 tenant_id 不改变身份租户。"""
    client, svc = _make_client()
    r = client.post("/api/tickets", json={
        "order_id": "ORD-1", "customer_id": "C1", "request_type": "refund",
        "reason": "商品破损", "reason_tags": ["damaged"], "idempotency_key": "tk-tenant-body",
        "tenant_id": "T2",                       # 攻击性字段：必须被忽略
    }, headers=_h("tok-agent-1"))
    assert r.status_code == 201
    t1 = r.json()["ticket_id"]
    assert r.json()["tenant_id"] == "T1"         # 仍按认证 T1 建单
    assert client.get(f"/api/tickets/{t1}", headers=_h("tok-agent-1")).status_code == 200
    assert client.get(f"/api/tickets/{t1}", headers=_h("tok-agent-2")).status_code == 403


def test_repeat_approve_click_is_invalid_not_double_effect():
    """重复点击审批：第二次无效（409）且不产生第二次副作用。"""
    client, svc = _make_client()
    t1 = _open_ticket(client, key="tk-2x").json()["ticket_id"]
    draft = client.post(f"/api/tickets/{t1}/refund-drafts", json={
        "amount": "60.00", "reason_detail": "x", "idempotency_key": "k-2x",
    }, headers=_h("tok-agent-1")).json()
    op_id = draft["operation_id"]
    client.post(f"/api/operations/{op_id}/submit", json={}, headers=_h("tok-agent-1"))
    first = client.post(f"/api/operations/{op_id}/approve", json={}, headers=_h("tok-approver-1"))
    assert first.status_code == 200
    second = client.post(f"/api/operations/{op_id}/approve", json={}, headers=_h("tok-approver-1"))
    assert second.status_code == 409
    assert second.json()["code"] == "AFTER_SALES_INVALID_STATE_TRANSITION"
    ex = client.post(f"/api/operations/{op_id}/execute",
                     json={"external_result": "success"}, headers=_h("tok-system-1"))
    assert ex.status_code == 200
    assert svc.refunded_amount("ORD-1").__str__() == "60.00"


def test_repeated_reconcile_after_settled_is_invalid():
    """unknown 收口一次后重复对账 → 409（不重复累计）。"""
    client, svc = _make_client()
    t1 = _open_ticket(client, key="tk-rc").json()["ticket_id"]
    draft = client.post(f"/api/tickets/{t1}/refund-drafts", json={
        "amount": "60.00", "reason_detail": "x", "idempotency_key": "k-rc",
    }, headers=_h("tok-agent-1")).json()
    op_id = draft["operation_id"]
    client.post(f"/api/operations/{op_id}/submit", json={}, headers=_h("tok-agent-1"))
    client.post(f"/api/operations/{op_id}/approve", json={}, headers=_h("tok-approver-1"))
    client.post(f"/api/operations/{op_id}/execute",
                json={"external_result": "timeout"}, headers=_h("tok-system-1"))
    ok = client.post(f"/api/operations/{op_id}/reconcile",
                     json={"result": "success"}, headers=_h("tok-system-1"))
    assert ok.status_code == 200
    again = client.post(f"/api/operations/{op_id}/reconcile",
                        json={"result": "success"}, headers=_h("tok-system-1"))
    assert again.status_code == 409
    assert again.json()["code"] == "AFTER_SALES_INVALID_STATE_TRANSITION"
    assert svc.refunded_amount("ORD-1").__str__() == "60.00"


def test_no_pii_in_audit_response():
    """理由中的手机号不进入审计响应（领域审计仅存动作/状态，note 不含正文）。"""
    client, _ = _make_client()
    r = client.post("/api/tickets", json={
        "order_id": "ORD-1", "customer_id": "C1", "request_type": "refund",
        "reason": "破损 电话 13812341234", "reason_tags": ["damaged"],
        "idempotency_key": "tk-pii",
    }, headers=_h("tok-agent-1"))
    assert r.status_code == 201
    t1 = r.json()["ticket_id"]
    audit = client.get(f"/api/audit?entity_type=ticket&entity_id={t1}",
                       headers=_h("tok-agent-1"))
    body = audit.text
    assert audit.status_code == 200
    assert "13812341234" not in body
    assert all(e.get("note") is None for e in audit.json())
