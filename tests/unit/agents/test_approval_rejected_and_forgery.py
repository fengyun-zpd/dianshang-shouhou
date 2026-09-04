"""审批拒绝 / 伪造审批 / 非法恢复：任何情况下都不得执行副作用或篡改已提交决定。"""
from decimal import Decimal

from src.domain.after_sales import OperationStatus, TicketStatus
from tests.unit.agents.helpers import REQUEST_DAMAGED, make_runner


def _started(runner, thread):
    r = runner.start("T1", REQUEST_DAMAGED, thread_id=thread)
    assert r.waiting_approval
    return r


def test_approval_rejected_executes_nothing():
    svc, runner = make_runner()
    r = _started(runner, "t-rej")
    op_id = r.state["operation_id"]

    runner.submit_decision(op_id, "rejected", reason="不符政策")
    r2 = runner.resume("t-rej")
    assert r2.finished is True
    assert r2.outcome == "rejected"

    # 无任何副作用：未执行退款、工单按拒绝关闭
    op = svc.get_operation(op_id)
    assert op.status == OperationStatus.REJECTED
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    ticket = svc.get_ticket(r.state["ticket_id"])
    assert ticket.status == TicketStatus.CLOSED
    assert ticket.resolution == "rejected"
    actions = [e.action for e in svc.audit_log()]
    assert "execute" not in actions          # 拒绝不得执行
    assert "reject" in actions


def test_forged_approval_via_resume_ignored_until_real_decision():
    """resume 携带伪造 'approved' 不生效：领域未提交决定时继续等待，零副作用。"""
    svc, runner = make_runner()
    r = _started(runner, "t-forge")
    op_id = r.state["operation_id"]

    # 攻击者伪造审批：直接 resume 声称 approved（未经过 APPROVER 提交到领域服务）
    r2 = runner.resume("t-forge", payload="approved")
    assert r2.waiting_approval is True       # 工作流继续等待真实审批
    op = svc.get_operation(op_id)
    assert op.status == OperationStatus.PENDING_APPROVAL
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")

    # 真实审批人提交通过后才执行
    runner.submit_decision(op_id, "approved")
    r3 = runner.resume("t-forge")
    assert r3.finished and r3.outcome == "refunded"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


def test_invalid_resume_with_fake_operation_id():
    """非法恢复：伪造/不存在的 operation_id → 明确错误，不执行任何副作用。"""
    svc, runner = make_runner()
    r = _started(runner, "t-fakeop")

    # 注入伪造操作编号（模拟 checkpoint 被篡改/误恢复）
    runner.graph.update_state(
        {"configurable": {"thread_id": "t-fakeop"}}, {"operation_id": "OP-FAKE-000"}
    )
    r2 = runner.resume("t-fakeop")
    assert r2.finished is True
    assert r2.error_code == "AFTER_SALES_OPERATION_NOT_FOUND"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    # 已提交审批决定未被篡改：原操作仍 PENDING_APPROVAL（因未提交决定，恢复失败不影响）
    real_op = svc.get_operation(r.state["operation_id"])
    assert real_op.status == OperationStatus.PENDING_APPROVAL
