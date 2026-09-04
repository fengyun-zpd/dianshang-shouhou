"""K6 security：安全不变量阻断问题必须为 0（集中攻击序列回归）。"""
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    ApproveCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    ReconcileCommand,
    RequestType,
    Role,
    SubmitCommand,
)
from tests.unit.domain.after_sales.helpers import service_with_policies

POL = (("P-DAMAGED-FULL", ("damaged",), "1.00", 30),)


def _base():
    svc = service_with_policies(*POL)
    ticket = svc.create_ticket(CreateTicketCommand(
        tenant_id="T1", order_id="ORD-1", customer_id="C1", request_type=RequestType.REFUND,
        reason="破损", reason_tags=("damaged",), actor=Role.AGENT, idempotency_key="sec-tk"))
    return svc, ticket


def _draft(svc, ticket_id, amount="50.00", key="sec-d"):
    return svc.create_refund(CreateRefundCommand(
        ticket_id, Decimal(amount), "x", Role.AGENT, key))


def test_privilege_escalation_is_zero():
    """阻断：越权成功 = 0。"""
    svc, ticket = _base()
    op = _draft(svc, ticket.ticket_id)
    svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    with pytest.raises(AfterSalesError) as e:
        svc.approve(ApproveCommand(op.operation_id, Role.AGENT, decision_version=1))  # Agent 冒充审批
    assert e.value.code == AfterSalesErrorCode.PERMISSION_DENIED
    with pytest.raises(AfterSalesError) as e2:
        svc.execute(ExecuteCommand(op.operation_id, Role.APPROVER))                    # 审批人冒充执行
    assert e2.value.code == AfterSalesErrorCode.PERMISSION_DENIED
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_repeated_side_effect_is_zero():
    """阻断：重复副作用 = 0（同键同载荷只一个对象，execute 只一次）。"""
    svc, ticket = _base()
    a = _draft(svc, ticket.ticket_id, "50.00", "sec-dup")
    b = _draft(svc, ticket.ticket_id, "50.00", "sec-dup")
    assert a.operation_id == b.operation_id
    svc.submit(SubmitCommand(a.operation_id, Role.AGENT))
    svc.approve(ApproveCommand(a.operation_id, Role.APPROVER, decision_version=1))
    svc.execute(ExecuteCommand(a.operation_id, Role.SYSTEM))
    with pytest.raises(AfterSalesError):
        svc.execute(ExecuteCommand(a.operation_id, Role.SYSTEM))   # 重复执行 → 非法迁移
    assert svc.refunded_amount("ORD-1") == Decimal("50.00")
    assert [e.action for e in svc.audit_log()].count("execute") == 1


def test_unknown_key_rotation_is_zero():
    """阻断：unknown 下换键重试 = 0（只能原键）。"""
    svc, ticket = _base()
    op = _draft(svc, ticket.ticket_id, "60.00", "sec-uk")
    svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=1))
    svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM, external_result="timeout"))
    with pytest.raises(AfterSalesError) as e:
        _draft(svc, ticket.ticket_id, "20.00", "sec-new-key")      # 换新键
    assert e.value.code == AfterSalesErrorCode.OPERATION_UNKNOWN_CONFLICT
    # 原键对账成功（不是重试）
    svc.reconcile(ReconcileCommand(op.operation_id, Role.SYSTEM, result="success"))
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")


def test_illegal_state_transition_is_zero():
    """阻断：非法状态迁移 = 0。"""
    svc, ticket = _base()
    op = _draft(svc, ticket.ticket_id)
    before = len(svc.audit_log())
    for bad in (
        lambda: svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM)),          # DRAFT
        lambda: svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=1)),  # 未提交
    ):
        with pytest.raises(AfterSalesError) as e:
            bad()
        assert e.value.code == AfterSalesErrorCode.INVALID_STATE_TRANSITION
    assert len(svc.audit_log()) == before
