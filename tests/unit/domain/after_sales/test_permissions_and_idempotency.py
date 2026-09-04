"""售后插件测试：权限矩阵、幂等、非法迁移与 operation_unknown 恢复。"""
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    ApproveCommand,
    CloseTicketCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    OperationStatus,
    ReconcileCommand,
    RequestType,
    Role,
    SubmitCommand,
)
from tests.unit.domain.after_sales.helpers import baseline_service, open_ticket


def _draft_and_pending(svc, ticket_id, key="k-p"):
    """返回已提交待审批的操作。"""
    op = svc.create_refund(CreateRefundCommand(
        ticket_id=ticket_id, amount=Decimal("50.00"),
        reason_detail="破损", actor=Role.AGENT, idempotency_key=key,
    ))
    return svc.submit(SubmitCommand(op.operation_id, Role.AGENT))


def test_customer_cannot_create_refund_draft():
    svc = baseline_service()
    ticket = open_ticket(svc)
    with pytest.raises(AfterSalesError) as e:
        svc.create_refund(CreateRefundCommand(
            ticket_id=ticket.ticket_id, amount=Decimal("50.00"),
            reason_detail="破损", actor=Role.CUSTOMER, idempotency_key="k-c",
        ))
    assert e.value.code == AfterSalesErrorCode.PERMISSION_DENIED


def test_agent_cannot_approve():
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = _draft_and_pending(svc, ticket.ticket_id)
    with pytest.raises(AfterSalesError) as e:
        svc.approve(ApproveCommand(op.operation_id, Role.AGENT, decision_version=1))
    assert e.value.code == AfterSalesErrorCode.PERMISSION_DENIED


def test_approver_cannot_execute():
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = _draft_and_pending(svc, ticket.ticket_id)
    svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=1))
    with pytest.raises(AfterSalesError) as e:
        svc.execute(ExecuteCommand(op.operation_id, Role.APPROVER))
    assert e.value.code == AfterSalesErrorCode.PERMISSION_DENIED


def test_system_cannot_approve():
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = _draft_and_pending(svc, ticket.ticket_id)
    with pytest.raises(AfterSalesError) as e:
        svc.approve(ApproveCommand(op.operation_id, Role.SYSTEM, decision_version=1))
    assert e.value.code == AfterSalesErrorCode.PERMISSION_DENIED


def test_customer_cannot_close_ticket():
    svc = baseline_service()
    ticket = open_ticket(svc)
    with pytest.raises(AfterSalesError) as e:
        svc.close_ticket(CloseTicketCommand(ticket.ticket_id, Role.CUSTOMER))
    assert e.value.code == AfterSalesErrorCode.PERMISSION_DENIED


def test_customer_can_create_ticket_but_not_close():
    """客户可录入诉求（只读入口），但写操作权限被拒绝。"""
    svc = baseline_service()
    ticket = open_ticket(svc, actor=Role.CUSTOMER, idempotency_key="tk-customer")
    assert ticket is not None
    with pytest.raises(AfterSalesError) as e:
        svc.close_ticket(CloseTicketCommand(ticket.ticket_id, Role.CUSTOMER))
    assert e.value.code == AfterSalesErrorCode.PERMISSION_DENIED


# ---------- 幂等 ----------

def test_idempotent_same_payload_returns_same_operation():
    svc = baseline_service()
    ticket = open_ticket(svc)
    first = svc.create_refund(CreateRefundCommand(
        ticket_id=ticket.ticket_id, amount=Decimal("50.00"),
        reason_detail="破损", actor=Role.AGENT, idempotency_key="dup",
    ))
    second = svc.create_refund(CreateRefundCommand(
        ticket_id=ticket.ticket_id, amount=Decimal("50.00"),
        reason_detail="破损", actor=Role.AGENT, idempotency_key="dup",
    ))
    assert first.operation_id == second.operation_id
    creates = [e for e in svc.audit_log() if e.action == "create_refund"]
    assert len(creates) == 1


def test_idempotent_ticket_same_payload_returns_same_ticket():
    svc = baseline_service()
    first = open_ticket(svc, idempotency_key="tk-dup")
    second = open_ticket(svc, idempotency_key="tk-dup")
    assert first.ticket_id == second.ticket_id


def test_idempotency_conflict_on_different_payload():
    svc = baseline_service()
    ticket = open_ticket(svc)
    svc.create_refund(CreateRefundCommand(
        ticket_id=ticket.ticket_id, amount=Decimal("10.00"),
        reason_detail="破损", actor=Role.AGENT, idempotency_key="same-key",
    ))
    with pytest.raises(AfterSalesError) as e:
        svc.create_refund(CreateRefundCommand(
            ticket_id=ticket.ticket_id, amount=Decimal("20.00"),
            reason_detail="破损", actor=Role.AGENT, idempotency_key="same-key",
        ))
    assert e.value.code == AfterSalesErrorCode.IDEMPOTENCY_CONFLICT


# ---------- 状态机 ----------

def test_approve_without_submit_invalid():
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = svc.create_refund(CreateRefundCommand(
        ticket_id=ticket.ticket_id, amount=Decimal("50.00"),
        reason_detail="破损", actor=Role.AGENT, idempotency_key="k-ns",
    ))
    with pytest.raises(AfterSalesError) as e:
        svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=1))
    assert e.value.code == AfterSalesErrorCode.INVALID_STATE_TRANSITION


def test_execute_before_approve_invalid():
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = _draft_and_pending(svc, ticket.ticket_id)
    with pytest.raises(AfterSalesError) as e:
        svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    assert e.value.code == AfterSalesErrorCode.INVALID_STATE_TRANSITION


def test_stale_decision_version_rejected():
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = _draft_and_pending(svc, ticket.ticket_id)
    with pytest.raises(AfterSalesError) as e:
        svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=99))
    assert e.value.code == AfterSalesErrorCode.DECISION_VERSION_MISMATCH


def test_cannot_execute_twice():
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = _draft_and_pending(svc, ticket.ticket_id)
    svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=1))
    svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    with pytest.raises(AfterSalesError) as e:
        svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    assert e.value.code == AfterSalesErrorCode.INVALID_STATE_TRANSITION


# ---------- operation_unknown ----------

def _enter_unknown(svc, ticket_id):
    op = _draft_and_pending(svc, ticket_id, key="k-unknown")
    svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=1))
    return svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM, external_result="timeout"))


def test_timeout_enters_unknown_and_blocks_new_key():
    """外部超时 → unknown；禁止换键重试，只能原键对账。"""
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = _enter_unknown(svc, ticket.ticket_id)
    assert op.status == OperationStatus.UNKNOWN
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")  # 金额未累计

    # 换新键新建退款 → 拒绝
    with pytest.raises(AfterSalesError) as e:
        svc.create_refund(CreateRefundCommand(
            ticket_id=ticket.ticket_id, amount=Decimal("20.00"),
            reason_detail="破损", actor=Role.AGENT, idempotency_key="k-new-key",
        ))
    assert e.value.code == AfterSalesErrorCode.OPERATION_UNKNOWN_CONFLICT

    # 对原操作再次 execute → 非法（unknown 只能对账收口）
    with pytest.raises(AfterSalesError) as e:
        svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    assert e.value.code == AfterSalesErrorCode.INVALID_STATE_TRANSITION


def test_reconcile_unknown_success_executes_and_audits():
    """unknown 对账成功 → executed 并累计退款，可正常关单。"""
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = _enter_unknown(svc, ticket.ticket_id)

    op = svc.reconcile(ReconcileCommand(op.operation_id, Role.SYSTEM, result="success"))
    assert op.status == OperationStatus.EXECUTED
    assert op.executed is True
    assert svc.refunded_amount("ORD-1") == Decimal("50.00")

    closed = svc.close_ticket(CloseTicketCommand(ticket.ticket_id, Role.AGENT))
    assert closed.status.value == "closed"
    assert [e.action for e in svc.audit_log()].count("reconcile_success") == 1


def test_reconcile_unknown_failed_terminal():
    """unknown 对账失败 → failed 终态；重复对账被拒绝。"""
    svc = baseline_service()
    ticket = open_ticket(svc)
    op = _enter_unknown(svc, ticket.ticket_id)

    op = svc.reconcile(ReconcileCommand(op.operation_id, Role.SYSTEM, result="failed"))
    assert op.status == OperationStatus.FAILED
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")

    with pytest.raises(AfterSalesError) as e:
        svc.reconcile(ReconcileCommand(op.operation_id, Role.SYSTEM, result="success"))
    assert e.value.code == AfterSalesErrorCode.INVALID_STATE_TRANSITION
