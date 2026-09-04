"""工作流正常路径：退款草稿 → 人工审批 interrupt → 通过 → 受控执行 → 关单与审计。"""
from decimal import Decimal

from src.domain.after_sales import OperationStatus, TicketStatus
from tests.unit.agents.helpers import REQUEST_DAMAGED, approve_and_resume, make_runner


def test_happy_path_refund_with_interrupt_resume():
    svc, runner = make_runner()

    # 1) 启动：草稿落库后进入人工审批 interrupt
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-happy")
    assert r.waiting_approval is True
    assert r.finished is False
    assert r.interrupt_value["type"] == "approval"

    ticket_id = r.state["ticket_id"]
    op_id = r.state["operation_id"]
    approval_id = r.state["approval_id"]
    assert approval_id != op_id  # 审批编号与操作编号独立

    # 2) 草稿已落库：操作处于 PENDING_APPROVAL（领域事实源）
    ops = svc.operations_of(ticket_id)
    assert len(ops) == 1
    assert ops[0].status == OperationStatus.PENDING_APPROVAL
    assert ops[0].amount == Decimal("100.00")
    # 建单/建草稿/提交已写审计
    audit_actions = [e.action for e in svc.audit_log()]
    assert "create_ticket" in audit_actions and "create_refund" in audit_actions and "submit" in audit_actions

    # 3) 授权人员提交决定（事实源）→ resume 恢复
    r2 = approve_and_resume(runner, "t-happy", op_id)
    assert r2.finished is True
    assert r2.outcome == "refunded"

    # 4) 最终业务状态：执行 + 关单
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    ticket = svc.get_ticket(ticket_id)
    assert ticket.status == TicketStatus.CLOSED
    assert ticket.resolution == "refunded"

    # 5) 审计：execute/close 均已写入，且事件 id 全部收集到状态
    tail = [e.action for e in svc.audit_log()]
    assert tail.count("execute") == 1
    assert "close_ticket" in tail
    assert any("execute" in eid for eid in r2.audit_event_ids)


def test_request_language_amount_is_ignored():
    """自然语言中的金额不生效：退款金额以领域政策计算为准（Agent 不自行决定）。"""
    svc, runner = make_runner()
    r = runner.start("T1", "订单 ORD-1 商品破损，退我 99999 元", thread_id="t-amount")
    assert r.waiting_approval
    assert r.state["action_draft"]["amount"] == "100.00"  # 政策全额 100，而非 99999

    approve_and_resume(runner, "t-amount", r.state["operation_id"])
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
