"""缺参澄清：缺少订单号 / 必要材料时 interrupt 澄清，补参后继续；澄清阶段无副作用。"""
from tests.unit.agents.helpers import make_runner


def test_missing_order_id_clarifies_then_continues():
    svc, runner = make_runner()
    r = runner.start("T1", "我要退款", thread_id="t-clarify-1")
    assert r.waiting_clarify is True
    assert any("订单号" in q for q in (r.interrupt_value or {}).get("questions", []))

    # 澄清阶段不得产生任何领域写副作用（无审计、无工单）
    assert svc.audit_log() == []

    # 用户补订单号与描述 → 回到 parse → 走到审批
    r2 = runner.resume("t-clarify-1", {"order_id": "ORD-1", "description": "商品破损"})
    assert r2.waiting_approval is True
    assert r2.state["operation_id"]
    assert svc.audit_log()  # 此时才落库


def test_missing_reason_material_clarifies():
    """有订单号但缺少问题描述（无法匹配政策证据）→ 澄清补材料。"""
    svc, runner = make_runner()
    r = runner.start("T1", "订单 ORD-1 我要退款", thread_id="t-clarify-2")
    assert r.waiting_clarify is True
    assert any("描述" in q for q in (r.interrupt_value or {}).get("questions", []))
    assert svc.audit_log() == []

    r2 = runner.resume("t-clarify-2", {"description": "外包装完好但内物破损"})
    assert r2.waiting_approval is True
    assert svc.operations_of(r2.state["ticket_id"])[0].status.value == "pending_approval"


def test_clarify_loop_keeps_waiting_until_sufficient():
    """补参仍不足（缺描述）→ 再次澄清，直到信息足够。"""
    svc, runner = make_runner()
    r = runner.start("T1", "我要退款", thread_id="t-clarify-3")
    assert r.waiting_clarify

    r2 = runner.resume("t-clarify-3", {"order_id": "ORD-1"})  # 仍无描述
    assert r2.waiting_clarify is True
    assert svc.audit_log() == []  # 始终未落库

    r3 = runner.resume("t-clarify-3", {"description": "商品破损"})
    assert r3.waiting_approval is True
