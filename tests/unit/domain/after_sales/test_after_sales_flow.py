"""售后插件测试：正常路径、部分退款上限、拒绝路径与关单守卫。"""
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    ApproveCommand,
    CloseTicketCommand,
    CreateRefundCommand,
    ExecuteCommand,
    OperationStatus,
    RejectCommand,
    Role,
    SubmitCommand,
    TicketStatus,
)
from tests.unit.domain.after_sales.helpers import baseline_service, open_ticket


def _refund_draft(svc, ticket_id, amount="100.00", key="k-refund-1", detail="破损全额退款"):
    return svc.create_refund(CreateRefundCommand(
        ticket_id=ticket_id,
        amount=Decimal(amount),
        reason_detail=detail,
        actor=Role.AGENT,
        idempotency_key=key,
    ))


def test_full_refund_happy_path_with_audit():
    """正常路径：建单 → 草稿 → 提交 → 审批 → 执行 → 关单，审计全程可追溯。"""
    svc = baseline_service()
    ticket = open_ticket(svc)
    assert ticket.status == TicketStatus.OPEN

    op = _refund_draft(svc, ticket.ticket_id)
    assert op.status == OperationStatus.DRAFT

    op = svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    assert op.status == OperationStatus.PENDING_APPROVAL

    op = svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=1))
    assert op.status == OperationStatus.APPROVED
    assert op.decision_version == 1

    op = svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    assert op.status == OperationStatus.EXECUTED
    assert op.executed is True
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")

    closed = svc.close_ticket(CloseTicketCommand(ticket.ticket_id, Role.AGENT))
    assert closed.status == TicketStatus.CLOSED
    assert closed.resolution == "refunded"

    actions = [e.action for e in svc.audit_log()]
    assert actions == [
        "create_ticket", "create_refund", "submit", "approve",
        "execute", "resolve_ticket", "close_ticket",
    ]


def test_partial_refund_twice_respects_cap():
    """退款上限：支持多次部分退款，累计不得超过实付。"""
    svc = baseline_service()
    ticket = open_ticket(svc)
    first = _refund_draft(svc, ticket.ticket_id, amount="60.00", key="k-60")
    svc.submit(SubmitCommand(first.operation_id, Role.AGENT))
    svc.approve(ApproveCommand(first.operation_id, Role.APPROVER, decision_version=1))
    svc.execute(ExecuteCommand(first.operation_id, Role.SYSTEM))
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")

    with pytest.raises(AfterSalesError) as e:
        _refund_draft(svc, ticket.ticket_id, amount="50.00", key="k-50-over")
    assert e.value.code == AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING

    second = _refund_draft(svc, ticket.ticket_id, amount="40.00", key="k-40-ok")
    svc.submit(SubmitCommand(second.operation_id, Role.AGENT))
    svc.approve(ApproveCommand(second.operation_id, Role.APPROVER, decision_version=1))
    svc.execute(ExecuteCommand(second.operation_id, Role.SYSTEM))
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


def test_close_blocked_while_operation_pending():
    """关单守卫：工单存在未决操作（草稿）时禁止关闭。"""
    svc = baseline_service()
    ticket = open_ticket(svc)
    _refund_draft(svc, ticket.ticket_id)  # 停留在 DRAFT

    with pytest.raises(AfterSalesError) as e:
        svc.close_ticket(CloseTicketCommand(ticket.ticket_id, Role.AGENT))
    assert e.value.code == AfterSalesErrorCode.TICKET_HAS_OPEN_OPERATIONS


def test_rejected_refund_closes_ticket_as_rejected():
    """审批拒绝路径：操作终态为 rejected，关单定性为 rejected。"""
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = _refund_draft(svc, ticket.ticket_id)
    svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    op = svc.reject(RejectCommand(op.operation_id, Role.APPROVER, reason="超出政策范围"))
    assert op.status == OperationStatus.REJECTED
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")

    closed = svc.close_ticket(CloseTicketCommand(ticket.ticket_id, Role.AGENT))
    assert closed.status == TicketStatus.CLOSED
    assert closed.resolution == "rejected"

    reject_audit = [e for e in svc.audit_log() if e.action == "reject"]
    assert reject_audit and reject_audit[-1].note == "超出政策范围"


def test_cannot_add_refund_after_ticket_closed():
    """工单关闭后禁止追加退款操作。"""
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = _refund_draft(svc, ticket.ticket_id)
    svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=1))
    svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    svc.close_ticket(CloseTicketCommand(ticket.ticket_id, Role.AGENT))

    with pytest.raises(AfterSalesError) as e:
        _refund_draft(svc, ticket.ticket_id, key="k-after-close")
    assert e.value.code == AfterSalesErrorCode.INVALID_STATE_TRANSITION
