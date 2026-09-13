"""HTTP Agent 生命周期端到端测试（AGENT_HTTP_RED）。

覆盖：start / clarify / decision / state 四个接口的正常路径与失败路径，
以及身份边界、跨租户、重复请求、伪造审批、未知状态、死循环等安全边界。

关键语义（与实现一致）：
- V1 只服务内部坐席：start/clarify 仅 AGENT；decision 仅 APPROVER/SYSTEM；
  state 允许 AGENT/APPROVER/SYSTEM；**同租户客户也被拒绝**（客户入口属规划能力）；
- decision 接口**不携带**审批结论：审批结果必须先经领域审批接口写入事实源；
  apply_decision 每次恢复都重读带版本的领域审批事实；
- 外部执行结果不由 HTTP 调用者指定：只能由 SYSTEM 角色的领域执行接口写入
  （/api/operations/{id}/execute），再由 decision 重读领域事实；
- start 请求体 forbidden extra：tenant_id / 金额 / 角色 / 审批结果 / 外部结果 → 422；
- 线程唯一键是 (tenant_id, thread_id)：不存在或错误租户统一 404，不暴露所属租户；
- checkpoint 只保存流程状态，业务最终事实仍以领域服务为准。
"""
from decimal import Decimal

from fastapi.testclient import TestClient

from src.agents import WorkflowRunner
from src.api import ApiIdentity, ApiTokenRegistry, create_app
from src.domain.after_sales import (
    Order,
    OrderItem,
    OrderStatus,
    PolicyRule,
    RequestType,
    Role,
)
from src.domain.after_sales.adapters import MemoryAdapter
from tests.unit.domain.after_sales.helpers import service_with_policies

POL = (("P-DAMAGED-FULL", ("damaged",), "1.00", 30),)
REQUEST = "订单 ORD-1 商品破损，要求退款"
REQUEST_B = "订单 ORD-2 商品破损，要求退款"
REQUEST_T2 = "订单 ORD-T2 商品破损，要求退款"
REQUEST_NO_ORDER = "我要退款，商品破损"


def _h(tok: str) -> dict:
    return {"X-Api-Key": tok}


def _client(max_steps: int = 32, demo_reset=None, agent_runner: bool = True):
    svc = service_with_policies(*POL)
    svc.seed_order(Order(
        order_id="ORD-2", tenant_id="T1", customer_id="C1",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("200.00"),
        items=[OrderItem(sku="S2", name="x", quantity=1, unit_price=Decimal("200.00"))],
        days_since_sign=1,
    ))
    svc.seed_order(Order(
        order_id="ORD-T2", tenant_id="T2", customer_id="C9",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("200.00"),
        items=[OrderItem(sku="S3", name="x", quantity=1, unit_price=Decimal("200.00"))],
        days_since_sign=1,
    ))
    svc.seed_policy(PolicyRule(
        policy_id="P-T2-DAMAGED", tenant_id="T2", request_type=RequestType.REFUND,
        reason_tags=("damaged",), window_days=30, refund_ratio=Decimal("1.00"),
    ))
    reg = ApiTokenRegistry()
    reg.register("tok-agent", ApiIdentity("agent-1", "T1", Role.AGENT))
    reg.register("tok-approver", ApiIdentity("approver-1", "T1", Role.APPROVER))
    reg.register("tok-system", ApiIdentity("system-1", "T1", Role.SYSTEM))
    reg.register("tok-customer", ApiIdentity("cust-1", "T1", Role.CUSTOMER, customer_id="C1"))
    reg.register("tok-agent-t2", ApiIdentity("agent-2", "T2", Role.AGENT))
    runner = WorkflowRunner(MemoryAdapter(svc), max_steps=max_steps) if agent_runner else None
    app = create_app(MemoryAdapter(svc), reg, demo_reset=demo_reset, agent_runner=runner)
    return TestClient(app), svc, runner


def _start(client, message=REQUEST, thread_id=None, order="ORD-1", tok="tok-agent"):
    body = {"message": message}
    if thread_id:
        body["thread_id"] = thread_id
    if order:
        body["order_id_hint"] = order
    return client.post("/api/v1/agent/start", json=body, headers=_h(tok))


def _approve(client, op_id, tok="tok-approver"):
    return client.post(f"/api/operations/{op_id}/approve", json={}, headers=_h(tok))


def _decide(client, thread_id, tok="tok-approver", body=None):
    return client.post(f"/api/v1/agent/{thread_id}/decision", json=body if body is not None else {},
                       headers=_h(tok))


# ---------- 1~6：正常闭环 ----------

def test_start_enters_approval_wait():
    client, svc, _ = _client()
    r = _start(client, thread_id="t-start")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["thread_id"] == "t-start"
    assert body["waiting_approval"] is True
    assert body["waiting_clarify"] is False
    assert body["finished"] is False
    assert body["operation_id"] and body["ticket_id"]
    assert body["next_action"] == "wait_approval"
    assert body["step_count"] > 0
    assert body["outcome"] is None, "等待审批不是终态"
    assert body["interrupt"]["type"] == "approval"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_start_without_order_id_enters_clarify():
    client, _, _ = _client()
    r = _start(client, message=REQUEST_NO_ORDER, thread_id="t-clarify", order=None)
    body = r.json()
    assert r.status_code == 200
    assert body["waiting_clarify"] is True
    assert body["waiting_approval"] is False
    assert body["operation_id"] is None
    assert body["interrupt"]["type"] == "clarify"
    assert body["interrupt"]["questions"]


def test_clarify_resumes_same_thread_then_waits_approval():
    client, svc, _ = _client()
    _start(client, message=REQUEST_NO_ORDER, thread_id="t-cl2", order=None)
    r = client.post("/api/v1/agent/t-cl2/clarify",
                    json={"message": "订单号是 ORD-1，商品破损", "order_id": "ORD-1",
                          "description": "商品破损"},
                    headers=_h("tok-agent"))
    body = r.json()
    assert r.status_code == 200
    assert body["thread_id"] == "t-cl2", "必须回到原线程继续执行"
    assert body["waiting_clarify"] is False
    assert body["waiting_approval"] is True
    assert body["operation_id"] and body["ticket_id"]
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_decision_empty_body_rereads_fact_and_executes():
    client, svc, _ = _client()
    op_id = _start(client, thread_id="t-ok").json()["operation_id"]

    # 4) 审批人通过现有领域接口写入审批事实（带版本）
    approved = _approve(client, op_id)
    assert approved.status_code == 200 and approved.json()["status"] == "approved"

    # 5) decision 空 body 只触发 apply_decision 重读
    r = _decide(client, "t-ok", body={})
    body = r.json()
    assert r.status_code == 200
    assert body["finished"] is True
    # 6) 最终进入执行
    assert body["outcome"] == "refunded"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


def test_decision_without_domain_decision_keeps_waiting():
    client, svc, _ = _client()
    _start(client, thread_id="t-wait")
    r = _decide(client, "t-wait")
    body = r.json()
    assert r.status_code == 200
    assert body["waiting_approval"] is True, "领域事实仍为待审批 → 工作流必须继续等待"
    assert body["finished"] is False
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_rejection_produces_no_refund_side_effect():
    client, svc, _ = _client()
    op_id = _start(client, thread_id="t-rej").json()["operation_id"]
    rej = client.post(f"/api/operations/{op_id}/reject",
                      json={"reason": "不符合政策"}, headers=_h("tok-approver"))
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"

    body = _decide(client, "t-rej").json()
    assert body["outcome"] == "rejected"
    assert body["finished"] is True
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    actions = [e.action for e in svc.audit_log()]
    assert not any(a.startswith("execute") for a in actions), "拒绝不得产生执行副作用"


# ---------- 8~9：state 查询与跨租户 ----------

def test_state_returns_thread_view():
    client, _, _ = _client()
    r = _start(client, thread_id="t-view")
    st = client.get("/api/v1/agent/t-view/state", headers=_h("tok-agent"))
    assert st.status_code == 200
    body = st.json()
    for key in ("thread_id", "waiting_approval", "waiting_clarify", "next_action",
                "operation_id", "ticket_id", "outcome", "error_code", "reply",
                "evidence_refs", "audit_event_ids", "step_count"):
        assert key in body, f"状态视图缺少字段 {key}"
    assert body["operation_id"] == r.json()["operation_id"]
    assert body["evidence_refs"], "只读证据引用应随流程记录"
    assert body["audit_event_ids"], "领域审计事件 id 应随流程记录"
    assert "checkpoint" in body["checkpoint_note"]


def test_cross_tenant_state_read_is_generic_404():
    """错误租户返回通用 404：不区分「不存在」与「属于其他租户」，也不暴露所属租户。"""
    client, _, _ = _client()
    _start(client, thread_id="t-x")
    denied = client.get("/api/v1/agent/t-x/state", headers=_h("tok-agent-t2"))
    assert denied.status_code == 404, "跨租户必须是通用 404（不是 403）"
    assert denied.json()["code"] == "AGENT_THREAD_NOT_FOUND"
    body = denied.text
    assert "T1" not in body, "错误响应不得泄露所属租户"
    assert "ORD-1" not in body and "OP-" not in body

    # 与「完全不存在的线程」响应结构一致（不可区分）
    missing = client.get("/api/v1/agent/no-such-thread/state", headers=_h("tok-agent-t2"))
    assert missing.status_code == 404
    assert missing.json()["code"] == denied.json()["code"]
    assert missing.json()["message"].replace("no-such-thread", "t-x") == \
        denied.json()["message"], "两种情况的错误消息除线程名外必须一致（不可区分）"

    # 跨租户续跑同样 404（且无副作用）
    assert client.post("/api/v1/agent/t-x/clarify", json={"order_id": "ORD-T2"},
                       headers=_h("tok-agent-t2")).status_code == 404
    assert client.get("/api/v1/agent/t-x/state", headers=_h("tok-agent-t2")).status_code == 404


def test_unknown_thread_state_is_404():
    client, _, _ = _client()
    r = client.get("/api/v1/agent/no-such-thread/state", headers=_h("tok-agent"))
    assert r.status_code == 404
    assert r.json()["code"] == "AGENT_THREAD_NOT_FOUND"


# ---------- 身份边界（V1 只服务内部坐席） ----------

def test_customer_cannot_use_agent_lifecycle():
    """同租户客户被拒绝：V1 的 Agent 生命周期只服务内部坐席（客户入口属规划能力）。"""
    client, svc, _ = _client()
    started = _start(client, thread_id="t-custguard")
    op_id = started.json()["operation_id"]

    for resp in (
        _start(client, thread_id="t-custstart", tok="tok-customer"),
        client.post("/api/v1/agent/t-custguard/clarify", json={"order_id": "ORD-1"},
                    headers=_h("tok-customer")),
        _decide(client, "t-custguard", tok="tok-customer"),
        client.get("/api/v1/agent/t-custguard/state", headers=_h("tok-customer")),
    ):
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "AFTER_SALES_PERMISSION_DENIED"

    # 客户被拒后无任何状态变化 / 副作用
    st = client.get("/api/v1/agent/t-custguard/state", headers=_h("tok-agent")).json()
    assert st["waiting_approval"] is True and st["operation_id"] == op_id
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_agent_cannot_call_decision():
    client, svc, _ = _client()
    _start(client, thread_id="t-perm")
    r = _decide(client, "t-perm", tok="tok-agent")
    assert r.status_code == 403
    assert r.json()["code"] == "AFTER_SALES_PERMISSION_DENIED"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_system_can_trigger_decision_after_domain_fact():
    """SYSTEM 也可触发恢复（但仍必须由领域事实决定结果）。"""
    client, svc, _ = _client()
    op_id = _start(client, thread_id="t-sys").json()["operation_id"]
    _approve(client, op_id)
    r = _decide(client, "t-sys", tok="tok-system")
    assert r.status_code == 200 and r.json()["outcome"] == "refunded"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


def test_state_allowed_for_approver_and_system():
    client, _, _ = _client()
    _start(client, thread_id="t-stateroles")
    for tok in ("tok-agent", "tok-approver", "tok-system"):
        assert client.get("/api/v1/agent/t-stateroles/state", headers=_h(tok)).status_code == 200


# ---------- 伪造审批 / 越权字段 ----------

def test_forged_decision_in_body_has_no_effect():
    client, svc, _ = _client()
    op_id = _start(client, thread_id="t-forge").json()["operation_id"]

    forged = _decide(client, "t-forge", body={"decision": "approved"})
    assert forged.status_code == 422, "decision 接口不得携带 approved/rejected/decision 字段"
    assert _decide(client, "t-forge", body={"approved": True}).status_code == 422

    st = client.get("/api/v1/agent/t-forge/state", headers=_h("tok-agent")).json()
    assert st["waiting_approval"] is True
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")

    # 只有领域事实写入 approve 后，空 body 的 decision 才让工作流进入执行
    _approve(client, op_id)
    assert _decide(client, "t-forge").json()["outcome"] == "refunded"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


def test_start_body_rejects_privileged_or_extra_fields():
    """start 请求体 extra="forbid"：租户/金额/角色/审批结果/外部结果一律 422。"""
    client, svc, _ = _client()
    for extra in ({"tenant_id": "T2"}, {"amount": "9999.00"}, {"role": "approver"},
                  {"approved": True}, {"decision": "approved"},
                  {"simulate_external": "timeout"}, {"external_result": "timeout"},
                  {"idempotency_key": "forged"}):
        body = {"message": REQUEST, "thread_id": "t-extra", "order_id_hint": "ORD-1"}
        body.update(extra)
        r = client.post("/api/v1/agent/start", json=body, headers=_h("tok-agent"))
        assert r.status_code == 422, f"{extra} 必须被拒绝（422），实际 {r.status_code}"
        assert r.json()["code"] == "VALIDATION_ERROR"
    # 被拒请求不产生任何线程与副作用
    assert client.get("/api/v1/agent/t-extra/state", headers=_h("tok-agent")).status_code == 404
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_clarify_cannot_smuggle_amount_or_status_fields():
    client, svc, _ = _client()
    _start(client, message=REQUEST_NO_ORDER, thread_id="t-smuggle", order=None)
    r = client.post("/api/v1/agent/t-smuggle/clarify",
                    json={"order_id": "ORD-1", "amount": "9999.00", "approved": True},
                    headers=_h("tok-agent"))
    assert r.status_code == 422, "澄清接口不允许额外字段（金额/审批/权限）"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_clarify_requires_meaningful_payload_and_clarify_state():
    client, _, _ = _client()
    _start(client, message=REQUEST_NO_ORDER, thread_id="t-cl3", order=None)
    empty = client.post("/api/v1/agent/t-cl3/clarify", json={}, headers=_h("tok-agent"))
    assert empty.status_code == 422, "空澄清载荷不推进工作流"

    # 非澄清状态（已进入审批等待）→ 稳定错误
    _start(client, thread_id="t-cl4")
    wrong = client.post("/api/v1/agent/t-cl4/clarify", json={"order_id": "ORD-1"},
                        headers=_h("tok-agent"))
    assert wrong.status_code == 409
    assert wrong.json()["code"] == "AGENT_NOT_WAITING_CLARIFY"


def test_decision_requires_approval_wait_state():
    client, _, _ = _client()
    _start(client, message=REQUEST_NO_ORDER, thread_id="t-dec-clarify", order=None)
    r = _decide(client, "t-dec-clarify")
    assert r.status_code == 409
    assert r.json()["code"] == "AGENT_NOT_WAITING_APPROVAL"


# ---------- 12：operation unknown（外部结果由 SYSTEM 写入领域事实） ----------

def test_operation_unknown_written_by_system_then_reread_by_decision():
    """未知状态必须由授权 SYSTEM 调用领域执行接口写入，再由 decision 重读事实。"""
    client, svc, _ = _client()
    started = _start(client, thread_id="t-unknown", order="ORD-1").json()
    op_id = started["operation_id"]
    _approve(client, op_id)

    # 授权 SYSTEM 写入 timeout → 领域事实 unknown
    ex = client.post(f"/api/operations/{op_id}/execute",
                     json={"external_result": "timeout"}, headers=_h("tok-system"))
    assert ex.status_code == 200 and ex.json()["status"] == "unknown"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")

    # decision 只触发重读 → operation_unknown，且不换键重试
    body = _decide(client, "t-unknown").json()
    assert body["outcome"] == "operation_unknown"
    assert body["next_action"] == "reconcile_required"
    assert "原操作" in body["reply"]
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")

    # 只能以原 operation_id 对账收口
    rec = client.post(f"/api/operations/{op_id}/reconcile",
                      json={"result": "success"}, headers=_h("tok-system"))
    assert rec.status_code == 200 and rec.json()["status"] == "executed"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


# ---------- 13：线程冲突 ----------

def test_same_thread_different_request_is_rejected():
    client, svc, _ = _client()
    _start(client, thread_id="t-conflict", order="ORD-1")
    before = len(svc.audit_log())
    r = _start(client, thread_id="t-conflict", order="ORD-2", message=REQUEST_B)
    assert r.status_code == 409
    assert r.json()["code"] == "AGENT_THREAD_CONFLICT"
    assert len(svc.audit_log()) == before, "被拒请求不得产生副作用"


def test_repeat_same_request_returns_original_thread_result():
    client, svc, _ = _client()
    first = _start(client, thread_id="t-repeat").json()
    before = len(svc.audit_log())
    again = _start(client, thread_id="t-repeat")
    assert again.status_code == 200
    assert again.json()["thread_id"] == first["thread_id"]
    assert again.json()["operation_id"] == first["operation_id"]
    assert len(svc.audit_log()) == before, "同请求重复提交不重放、无新副作用"


def test_same_thread_name_in_two_tenants_does_not_conflict():
    """同名线程在不同租户下互不冲突（唯一键 = (tenant_id, thread_id)）。"""
    client, svc, _ = _client()
    t1 = _start(client, thread_id="same-name", order="ORD-1").json()
    t2_resp = _start(client, message=REQUEST_T2, thread_id="same-name",
                     order="ORD-T2", tok="tok-agent-t2")
    assert t2_resp.status_code == 200, t2_resp.text
    t2 = t2_resp.json()
    assert t2["thread_id"] == "same-name"
    assert t2["state"]["tenant_id"] == "T2"
    assert t2["operation_id"] != t1["operation_id"]

    # 各自读自己的线程，互不可见
    s1 = client.get("/api/v1/agent/same-name/state", headers=_h("tok-agent")).json()
    s2 = client.get("/api/v1/agent/same-name/state", headers=_h("tok-agent-t2")).json()
    assert s1["operation_id"] == t1["operation_id"]
    assert s2["operation_id"] == t2["operation_id"]
    assert s1["state"]["tenant_id"] == "T1" and s2["state"]["tenant_id"] == "T2"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    assert svc.refunded_amount("ORD-T2") == Decimal("0.00")


# ---------- 14~15：reset 路由 profile 边界 ----------

def test_memory_profile_registers_demo_reset():
    calls = []
    client, _, _ = _client(demo_reset=lambda: calls.append(1))
    r = client.post("/api/demo/reset", headers=_h("tok-agent"))
    assert r.status_code == 200 and r.json()["status"] == "reset"
    assert calls == [1]


def test_pg_profile_does_not_expose_memory_reset():
    client, _, _ = _client()          # demo_reset=None（pg profile 语义）
    r = client.post("/api/demo/reset", headers=_h("tok-agent"))
    assert r.status_code == 404, "pg profile 下 memory reset 路由根本不注册"


def test_missing_runner_returns_503():
    client, _, _ = _client(agent_runner=False)
    r = _start(client, thread_id="t-norunner")
    assert r.status_code == 503
    assert r.json()["code"] == "AGENT_RUNNER_UNAVAILABLE"


# ---------- 16：死循环保护 ----------

def test_loop_detection_returns_stable_error_code():
    client, svc, _ = _client(max_steps=8)
    _start(client, thread_id="t-loop")
    assert _decide(client, "t-loop").json()["waiting_approval"] is True
    body = _decide(client, "t-loop").json()
    assert body["error_code"] == "AGENT_LOOP_DETECTED"
    assert body["outcome"] == "escalated"
    assert body["finished"] is True
    assert "转人工" in body["reply"]
    # 触发后无新增领域写入、无退款执行
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    assert not any(e.action.startswith("execute") for e in svc.audit_log())

    # 安全停止后的状态视图一致，不再表现为「等待中」
    st = client.get("/api/v1/agent/t-loop/state", headers=_h("tok-agent")).json()
    assert st["error_code"] == "AGENT_LOOP_DETECTED"
    assert st["waiting_approval"] is False
    assert st["finished"] is True


# ---------- 认证 ----------

def test_agent_endpoints_require_authentication():
    client, _, _ = _client()
    assert client.post("/api/v1/agent/start", json={"message": REQUEST}).status_code == 401
    assert client.get("/api/v1/agent/x/state").status_code == 401
    assert client.post("/api/v1/agent/x/decision", json={}).status_code == 401
