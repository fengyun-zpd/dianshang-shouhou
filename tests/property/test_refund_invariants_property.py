"""K6 性质矩阵：确定性遍历退款引擎不变量。

不变量：已执行累计 ≤ 实付；非法迁移零副作用；草稿级金额也必须 ≤ 实付。
"""
import itertools
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    ApproveCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    OperationStatus,
    ReconcileCommand,
    RequestType,
    Role,
    SubmitCommand,
)
from src.domain.after_sales.models import Order, OrderItem, OrderStatus
from src.domain.after_sales.policies import PolicyRule
from src.domain.after_sales.service import AfterSalesService

PAID = ["50.00", "100.00", "1000.00"]
FRACTIONS = [("0.60", "0.60"), ("0.30", "0.30"), ("0.60", "0.30")]


def _svc(paid: str):
    svc = AfterSalesService()
    svc.seed_order(Order(
        order_id="ORD-1", tenant_id="T1", customer_id="C1", status=OrderStatus.DELIVERED,
        paid_amount=Decimal(paid),
        items=[OrderItem(sku="S1", name="x", quantity=1, unit_price=Decimal(paid))],
        days_since_sign=1,
    ))
    svc.seed_policy(PolicyRule("P-FULL", "T1", RequestType.REFUND, ("damaged",), 30, Decimal("1.00")))
    ticket = svc.create_ticket(CreateTicketCommand(
        tenant_id="T1", order_id="ORD-1", customer_id="C1", request_type=RequestType.REFUND,
        reason="破损", reason_tags=("damaged",), actor=Role.AGENT, idempotency_key="pv-tk"))
    return svc, ticket


def _mk_op(svc, ticket_id, amount: str, key: str):
    return svc.create_refund(CreateRefundCommand(
        ticket_id, Decimal(amount), "x", Role.AGENT, key))


def _approve(svc, op_id):
    op = svc.get_operation(op_id)
    svc.approve(ApproveCommand(op_id, Role.APPROVER, decision_version=op.version))


@pytest.mark.parametrize("paid,f1,f2",
                         [(p, x, y) for p in PAID for (x, y) in FRACTIONS])
def test_executed_total_never_exceeds_paid(paid, f1, f2):
    svc, ticket = _svc(paid)
    paid_dec = Decimal(paid)
    a1 = (paid_dec * Decimal(f1)).quantize(Decimal("0.01"))
    a2 = (paid_dec * Decimal(f2)).quantize(Decimal("0.01"))
    op1 = _mk_op(svc, ticket.ticket_id, str(a1), "pv-a")
    op2 = _mk_op(svc, ticket.ticket_id, str(a2), "pv-b")
    for op in (op1, op2):
        svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    _approve(svc, op1.operation_id)
    _approve(svc, op2.operation_id)

    succeeded = 0
    for op in (op1, op2):
        try:
            svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
            succeeded += 1
        except AfterSalesError as e:
            assert e.code == AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING

    total = svc.refunded_amount("ORD-1")
    assert total <= paid_dec                    # 不变量：不超付
    if a1 + a2 <= paid_dec:
        assert succeeded == 2 and total == a1 + a2
    else:
        assert succeeded == 1 and total == a1   # 第一笔成功、第二笔被原子拒绝


def test_draft_amount_must_not_exceed_paid():
    svc, ticket = _svc("50.00")
    with pytest.raises(AfterSalesError) as e:
        _mk_op(svc, ticket.ticket_id, "60.00", "pv-over")
    assert e.value.code == AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING


def test_unknown_reconcile_blocks_other_execution_when_over_paid():
    svc, ticket = _svc("100.00")
    op1 = _mk_op(svc, ticket.ticket_id, "60.00", "pv-u1")
    op2 = _mk_op(svc, ticket.ticket_id, "60.00", "pv-u2")
    for op in (op1, op2):
        svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    _approve(svc, op1.operation_id)
    _approve(svc, op2.operation_id)
    svc.execute(ExecuteCommand(op1.operation_id, Role.SYSTEM, external_result="timeout"))
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")

    svc.reconcile(ReconcileCommand(op1.operation_id, Role.SYSTEM, result="success"))
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")
    with pytest.raises(AfterSalesError) as e:
        svc.execute(ExecuteCommand(op2.operation_id, Role.SYSTEM))
    assert e.value.code == AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")


def test_illegal_transition_never_mutates_state():
    svc, ticket = _svc("100.00")
    op = _mk_op(svc, ticket.ticket_id, "50.00", "pv-i1")   # DRAFT
    before = svc.export_state()
    with pytest.raises(AfterSalesError) as e:
        svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    assert e.value.code == AfterSalesErrorCode.INVALID_STATE_TRANSITION
    assert svc.export_state() == before
