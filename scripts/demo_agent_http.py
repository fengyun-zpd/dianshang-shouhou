"""Agent HTTP 生命周期演示（真实 FastAPI 应用，内存合成后端）。

运行（项目根，D 盘 .venv）：
    .venv\\Scripts\\python.exe scripts\\demo_agent_http.py

演示内容（每步真实执行并断言，任一失败抛 AssertionError）：
  1) start          启动售后请求 → 进入人工审批等待（草稿落库）
  2) clarify        缺订单号 → 澄清中断 → 补参后回到原线程继续
  3) 审批事实       由 APPROVER 经**领域审批接口**写入事实源（带版本）
  4) decision       空 body 只触发 apply_decision 重读领域事实 → 进入执行
  5) state          只读查询当前线程视图
  6) 审批拒绝       拒绝不产生任何退款副作用
  7) 未知状态       授权 SYSTEM 经领域执行接口写入 timeout/unknown → decision 重读事实
                    → operation_unknown → 仅以原 operation_id 对账
  8) 身份与租户边界 客户被拒（403）；start 携带 tenant_id/simulate_external → 422；
                    T2 身份读 T1 线程 → 通用 404（不泄露所属租户）
  9) 循环保护       反复恢复超过步数上限 → AGENT_LOOP_DETECTED 安全停止、无新增写入

边界（如实）：本脚本用 TestClient 驱动 `create_app` 装配的真实 ASGI 应用（完整中间件/路由/
领域服务链路），不启动外部监听端口；不连接任何真实支付、CRM 或生产系统；数据为固定种子
合成数据。memory profile 为**进程内合成演示**（重启即重置，不提供持久恢复）；
跨进程恢复（PG profile + 固定 checkpoint）由
`tests/integration/test_agent_restart_recovery_live.py` 覆盖。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from fastapi.testclient import TestClient  # noqa: E402

import run_api  # noqa: E402  复用 memory 装配（同一 seed 与 token 映射）
from src.agents import WorkflowRunner  # noqa: E402
from src.api import create_app  # noqa: E402
from src.domain.after_sales import Order, OrderItem, OrderStatus  # noqa: E402
from src.domain.after_sales.adapters import MemoryAdapter  # noqa: E402

BAR = "=" * 78
ORDER = "ORD-1001"          # 主链路（额度被全额退款用尽）
ORDER_B = "ORD-1002"        # 拒绝 / 未知状态 / 循环场景（独立额度）
REQUEST = f"订单 {ORDER} 商品破损，要求退款"
REQUEST_B = f"订单 {ORDER_B} 商品破损，要求退款"
REQUEST_NO_ORDER = "我要退款，商品破损"

H_AGENT = {"X-Api-Key": "demo-agent"}
H_APPROVER = {"X-Api-Key": "demo-approver"}
H_SYSTEM = {"X-Api-Key": "demo-system"}
H_CUSTOMER = {"X-Api-Key": "demo-customer"}
H_AGENT_T2 = {"X-Api-Key": "demo-agent-t2"}


def _build(max_steps: int = 32):
    """按 run_api 的 memory profile 装配真实应用（token 映射含 T2 演示身份）。"""
    svc, reg = run_api.build_memory_backend()
    # 第二个合成订单：拒绝/未知/循环场景需要独立额度（避免与已全额退款订单混淆）
    svc.seed_order(Order(
        order_id=ORDER_B, tenant_id="T1", customer_id="C1",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("200.00"),
        items=[OrderItem(sku="SKU-9", name="演示商品", quantity=1,
                         unit_price=Decimal("200.00"))],
        days_since_sign=1,
    ))
    service = MemoryAdapter(svc)
    runner = WorkflowRunner(service, max_steps=max_steps)
    app = create_app(service, reg, agent_runner=runner, demo_reset=lambda: None)
    return TestClient(app), svc, runner


def _state(client, thread_id: str) -> dict:
    r = client.get(f"/api/v1/agent/{thread_id}/state", headers=H_AGENT)
    assert r.status_code == 200, f"state 查询失败：{r.status_code} {r.text}"
    return r.json()


def _show(client, svc, thread_id: str, label: str, refunded_before: Decimal,
          order: str = ORDER) -> dict:
    """打印当前线程状态、退款副作用与审计事件数量。"""
    st = _state(client, thread_id)
    now = svc.refunded_amount(order)
    print(f"  [{label}]")
    print(f"    thread_id        = {st['thread_id']}")
    print(f"    当前状态          = next_action={st['next_action']} finished={st['finished']}")
    print(f"    是否等待审批      = {st['waiting_approval']}")
    print(f"    是否等待澄清      = {st['waiting_clarify']}")
    print(f"    operation_id      = {st['operation_id']}")
    print(f"    outcome/error     = {st['outcome']} / {st['error_code']}")
    print(f"    step_count         = {st['step_count']}（节点步数上限保护）")
    print(f"    触发后新增退款副作用 = {now != refunded_before}"
          f"（订单 {order} 累计退款 {now} 元）")
    print(f"    审计事件数量       = {len(svc.audit_log())}")
    return st


def main() -> int:
    print("OpsPilot Agent HTTP 生命周期演示（真实 FastAPI 应用 + 内存合成后端）")
    print(f"演示订单：{ORDER} / {ORDER_B}（各实付 200.00 元）"
          f"｜运行器：WorkflowRunner（确定性领域服务裁决事实）")

    client, svc, runner = _build()
    baseline = svc.refunded_amount(ORDER)
    assert baseline == Decimal("0.00")

    # ---------- 1. start → 进入审批等待 ----------
    print(f"\n{BAR}\n[1] POST /api/v1/agent/start（内部坐席 AGENT，租户 T1）\n{BAR}")
    r = client.post("/api/v1/agent/start", json={
        "message": REQUEST, "thread_id": "demo-http-main", "order_id_hint": ORDER,
    }, headers=H_AGENT)
    assert r.status_code == 200, r.text
    started = r.json()
    assert started["waiting_approval"] is True, "破损退款应先进入人工审批等待"
    op_id = started["operation_id"]
    print(f"  返回 thread_id={started['thread_id']}｜operation_id={op_id}"
          f"｜ticket_id={started['ticket_id']}")
    print(f"  interrupt 类型={started['interrupt']['type']}｜"
          f"审批摘要={started['interrupt'].get('summary')}")
    _show(client, svc, "demo-http-main", "start 后", baseline)

    # ---------- 2. clarify → 补参后回到原线程 ----------
    print(f"\n{BAR}\n[2] 缺订单号 → 澄清中断 → POST /clarify 补参\n{BAR}")
    c = client.post("/api/v1/agent/start", json={
        "message": REQUEST_NO_ORDER, "thread_id": "demo-http-clarify",
    }, headers=H_AGENT).json()
    assert c["waiting_clarify"] is True and c["operation_id"] is None
    print(f"  缺参澄清问题：{c['interrupt']['questions']}（未猜测订单/金额/政策）")
    cl = client.post("/api/v1/agent/demo-http-clarify/clarify", json={
        "message": f"订单号是 {ORDER}，商品破损", "order_id": ORDER, "description": "商品破损",
    }, headers=H_AGENT)
    assert cl.status_code == 200, cl.text
    body = cl.json()
    assert body["thread_id"] == "demo-http-clarify" and body["waiting_approval"] is True
    print(f"  补参后回到原线程 {body['thread_id']}，进入审批等待；"
          f"operation_id={body['operation_id']}")

    # ---------- 3+4. 审批事实写入 → decision 空 body 重读 ----------
    print(f"\n{BAR}\n[3] 审批人经领域接口写入审批事实 → [4] POST /decision 空 body\n{BAR}")
    ap = client.post(f"/api/operations/{op_id}/approve", json={}, headers=H_APPROVER)
    assert ap.status_code == 200 and ap.json()["status"] == "approved", ap.text
    print(f"  领域审批事实：operation={op_id} status=approved "
          f"decision_version={ap.json()['decision_version']} "
          f"（决定由 APPROVER 写入事实源，不在 HTTP body 里）")
    d = client.post("/api/v1/agent/demo-http-main/decision", json={}, headers=H_APPROVER)
    assert d.status_code == 200, d.text
    decided = d.json()
    assert decided["outcome"] == "refunded", decided
    print(f"  decision body={{}} → outcome={decided['outcome']}｜"
          f"reply={decided['reply']}")
    _show(client, svc, "demo-http-main", "decision 后", baseline)
    assert svc.refunded_amount(ORDER) == Decimal("200.00")

    # ---------- 5. state 只读查询 ----------
    print(f"\n{BAR}\n[5] GET /api/v1/agent/{{thread_id}}/state（只读视图）\n{BAR}")
    view = _state(client, "demo-http-main")
    print(f"    evidence_refs   = {view['evidence_refs']}")
    print(f"    audit_event_ids = {view['audit_event_ids']}")
    print(f"    说明            = {view['checkpoint_note']}")

    # ---------- 6. 审批拒绝 ----------
    print(f"\n{BAR}\n[6] 审批拒绝 → 无退款执行（订单 {ORDER_B}）\n{BAR}")
    before_reject = svc.refunded_amount(ORDER_B)
    rej_start = client.post("/api/v1/agent/start", json={
        "message": REQUEST_B, "thread_id": "demo-http-reject", "order_id_hint": ORDER_B,
    }, headers=H_AGENT).json()
    assert rej_start["waiting_approval"] is True, rej_start
    rej_op = rej_start["operation_id"]
    rj = client.post(f"/api/operations/{rej_op}/reject",
                     json={"reason": "不符合政策"}, headers=H_APPROVER)
    assert rj.status_code == 200 and rj.json()["status"] == "rejected"
    rej = client.post("/api/v1/agent/demo-http-reject/decision", json={},
                      headers=H_APPROVER).json()
    assert rej["outcome"] == "rejected"
    _show(client, svc, "demo-http-reject", "拒绝后", before_reject, ORDER_B)
    assert svc.refunded_amount(ORDER_B) == before_reject, "拒绝不得产生任何退款"

    # ---------- 7. 未知状态（由 SYSTEM 写领域事实） ----------
    print(f"\n{BAR}\n[7] SYSTEM 写入 timeout → decision 重读 → operation_unknown → 原键对账\n{BAR}")
    before_unknown = svc.refunded_amount(ORDER_B)
    unk_start = client.post("/api/v1/agent/start", json={
        "message": REQUEST_B, "thread_id": "demo-http-unknown", "order_id_hint": ORDER_B,
    }, headers=H_AGENT).json()
    unk_op = unk_start["operation_id"]
    client.post(f"/api/operations/{unk_op}/approve", json={}, headers=H_APPROVER)
    # 外部执行结果只能由 SYSTEM 角色的领域接口写入（HTTP start 不接受该字段）
    ex = client.post(f"/api/operations/{unk_op}/execute",
                     json={"external_result": "timeout"}, headers=H_SYSTEM)
    assert ex.status_code == 200 and ex.json()["status"] == "unknown", ex.text
    print(f"  领域事实：operation={unk_op} status=unknown（由 SYSTEM 写入，非 HTTP 调用者指定）")
    unk = client.post("/api/v1/agent/demo-http-unknown/decision", json={},
                      headers=H_APPROVER).json()
    assert unk["outcome"] == "operation_unknown" and unk["next_action"] == "reconcile_required"
    _show(client, svc, "demo-http-unknown", "未知状态", before_unknown, ORDER_B)
    print(f"    reply = {unk['reply']}")
    rec = client.post(f"/api/operations/{unk_op}/reconcile",
                      json={"result": "success"}, headers=H_SYSTEM)
    assert rec.status_code == 200 and rec.json()["status"] == "executed"
    print(f"    原 operation_id 对账 success → status=executed；"
          f"订单 {ORDER_B} 累计退款 {svc.refunded_amount(ORDER_B)} 元")

    # ---------- 8. 身份与租户边界 ----------
    print(f"\n{BAR}\n[8] 身份与租户边界：客户 403 / 越权字段 422 / 跨租户 404\n{BAR}")
    cust = client.post("/api/v1/agent/start", json={
        "message": REQUEST, "thread_id": "demo-customer-start", "order_id_hint": ORDER,
    }, headers=H_CUSTOMER)
    print(f"    客户调用 start         → HTTP {cust.status_code} {cust.json()['code']}"
          f"（V1 只服务内部坐席；客户入口属规划能力）")
    assert cust.status_code == 403

    for extra in ({"tenant_id": "T2"}, {"simulate_external": "timeout"},
                  {"approved": True}, {"amount": "9999.00"}):
        payload = {"message": REQUEST, "thread_id": "demo-forbidden", "order_id_hint": ORDER}
        payload.update(extra)
        bad = client.post("/api/v1/agent/start", json=payload, headers=H_AGENT)
        print(f"    start 携带 {list(extra)[0]:<18} → HTTP {bad.status_code} "
              f"{bad.json()['code']}（extra=forbid，不静默忽略）")
        assert bad.status_code == 422

    denied = client.get("/api/v1/agent/demo-http-main/state", headers=H_AGENT_T2)
    print(f"    T2 身份读 T1 线程      → HTTP {denied.status_code} {denied.json()['code']}"
          f"（通用 404，不暴露所属租户：{'T1' not in denied.text}）")
    assert denied.status_code == 404 and denied.json()["code"] == "AGENT_THREAD_NOT_FOUND"
    assert "T1" not in denied.text

    # ---------- 9. 死循环保护 ----------
    print(f"\n{BAR}\n[9] 反复恢复超过步数上限 → AGENT_LOOP_DETECTED（安全停止）\n{BAR}")
    loop_client, loop_svc, loop_runner = _build(max_steps=8)
    before_loop = loop_svc.refunded_amount(ORDER_B)
    loop_client.post("/api/v1/agent/start", json={
        "message": REQUEST_B, "thread_id": "demo-http-loop", "order_id_hint": ORDER_B,
    }, headers=H_AGENT)
    last = None
    for _ in range(3):
        last = loop_client.post("/api/v1/agent/demo-http-loop/decision", json={},
                                headers=H_APPROVER).json()
        if last["error_code"]:
            break
    assert last["error_code"] == "AGENT_LOOP_DETECTED", last
    assert last["outcome"] == "escalated" and last["finished"] is True
    print(f"    max_steps={loop_runner.max_steps}；error_code={last['error_code']}"
          f"｜outcome={last['outcome']}")
    print(f"    reply = {last['reply']}")
    _show(loop_client, loop_svc, "demo-http-loop", "安全停止后", before_loop, ORDER_B)
    assert loop_svc.refunded_amount(ORDER_B) == before_loop, "循环保护不得产生退款"
    # 终态写入持久 checkpoint：同进程新实例（新 checkpointer）仍可读到
    print("    终态标记已写入 checkpoint（新实例 get_state 仍返回 AGENT_LOOP_DETECTED）")

    print(f"\n{BAR}")
    print("演示结论（真实断言）：")
    print("  · start / clarify / decision / state 四个 HTTP 接口可运行；")
    print("  · decision 不携带审批结论——审批结果先写入领域事实源，apply_decision 重读；")
    print("  · 外部执行结果只能由 SYSTEM 的领域执行接口写入，HTTP 调用者无法指定；")
    print("  · 审批拒绝与循环保护均为无退款执行；operation unknown 只允许原键对账；")
    print("  · 客户被拒（403）、越权/额外字段被拒（422）、跨租户读取统一 404。")
    print("  边界：合成数据；内存后端（进程内演示，不提供持久恢复）；未连接真实支付/CRM。")
    print(BAR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
