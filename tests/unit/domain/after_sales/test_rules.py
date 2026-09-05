"""确定性规则纯函数单测（rules.py：无副作用、稳定错误码、不依赖 LLM/存储）。"""
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    OperationStatus,
    Order,
    OrderItem,
    OrderStatus,
    Role,
    TicketStatus,
)
from src.domain.after_sales.rules import (
    check_operation_transition,
    check_ticket_transition,
    has_unknown_on_order,
    idempotency_hit_ok,
    require_role,
    ticket_close_qualification,
    validate_amount_positive,
    validate_customer_order_match,
    validate_decision_version,
    validate_order_access,
    validate_refund_capacity,
)
from src.domain.idempotency import IdempotencyRecord, PENDING_REFUND_ID


def _order(order_id="ORD-1", tenant="T1", customer="C1", paid="100.00") -> Order:
    return Order(order_id=order_id, tenant_id=tenant, customer_id=customer,
                 status=OrderStatus.DELIVERED, paid_amount=Decimal(paid),
                 items=[OrderItem(sku="S", name="x", quantity=1, unit_price=Decimal(paid))],
                 days_since_sign=1)


def test_require_role():
    require_role(Role.AGENT, (Role.AGENT, Role.CUSTOMER), "m")
    with pytest.raises(AfterSalesError) as ei:
        require_role(Role.APPROVER, (Role.AGENT,), "只有 Agent 可以")
    assert ei.value.code == AfterSalesErrorCode.PERMISSION_DENIED


def test_validate_order_access():
    o = _order()
    validate_order_access(o, "T1")
    with pytest.raises(AfterSalesError) as ei:
        validate_order_access(None, "T1")
    assert ei.value.code == AfterSalesErrorCode.ORDER_NOT_FOUND
    with pytest.raises(AfterSalesError) as ei:
        validate_order_access(o, "T2")
    assert ei.value.code == AfterSalesErrorCode.TENANT_MISMATCH
    closed = _order(order_id="ORD-X")
    closed.status = OrderStatus.CLOSED
    with pytest.raises(AfterSalesError) as ei:
        validate_order_access(closed, "T1")
    assert ei.value.code == AfterSalesErrorCode.ORDER_STATUS_NOT_ELIGIBLE


def test_validate_customer_order_match():
    o = _order(customer="C1")
    validate_customer_order_match(o, "C1")
    with pytest.raises(AfterSalesError) as ei:
        validate_customer_order_match(o, "C2")     # 客户借用他客户订单 → 拒绝
    assert ei.value.code == AfterSalesErrorCode.PERMISSION_DENIED
    with pytest.raises(AfterSalesError) as ei:
        validate_customer_order_match(None, "C1")
    assert ei.value.code == AfterSalesErrorCode.PERMISSION_DENIED


def test_amount_and_capacity():
    validate_amount_positive(Decimal("0.01"))
    with pytest.raises(AfterSalesError) as ei:
        validate_amount_positive(Decimal("0"))
    assert ei.value.code == AfterSalesErrorCode.AMOUNT_NOT_POSITIVE
    validate_refund_capacity(Decimal("100.00"), Decimal("60.00"), Decimal("40.00"))
    with pytest.raises(AfterSalesError) as ei:
        validate_refund_capacity(Decimal("100.00"), Decimal("60.00"), Decimal("40.01"))
    assert ei.value.code == AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING


def test_state_transitions_table_driven():
    check_operation_transition(OperationStatus.DRAFT, OperationStatus.PENDING_APPROVAL)
    with pytest.raises(AfterSalesError) as ei:
        check_operation_transition(OperationStatus.EXECUTED, OperationStatus.DRAFT)  # 终态不可逆
    assert ei.value.code == AfterSalesErrorCode.INVALID_STATE_TRANSITION
    check_ticket_transition(TicketStatus.OPEN, TicketStatus.RESOLVED)
    check_ticket_transition(TicketStatus.RESOLVED, TicketStatus.CLOSED)
    with pytest.raises(AfterSalesError):
        check_ticket_transition(TicketStatus.CLOSED, TicketStatus.OPEN)


def test_decision_version_cas():
    validate_decision_version(1, 1)
    with pytest.raises(AfterSalesError) as ei:
        validate_decision_version(2, 1)   # 过期 expected_version
    assert ei.value.code == AfterSalesErrorCode.DECISION_VERSION_MISMATCH


def test_idempotency_hit_ok():
    rec = IdempotencyRecord(payload_hash="h1", refund_id="TKT-1")
    assert idempotency_hit_ok(rec, "h1") is True
    assert idempotency_hit_ok(rec, "h2") is False            # 异载荷
    pending = IdempotencyRecord(payload_hash="h1", refund_id=PENDING_REFUND_ID)
    assert idempotency_hit_ok(pending, "h1") is False        # 占位不算命中
    assert idempotency_hit_ok(None, "h1") is False


def test_unknown_on_order_guard():
    class _Op:
        def __init__(self, order_id, status):
            self.order_id = order_id
            self.status = status

    ops = [_Op("ORD-1", OperationStatus.APPROVED), _Op("ORD-1", OperationStatus.UNKNOWN)]
    assert has_unknown_on_order(ops, "ORD-1") is True
    assert has_unknown_on_order(ops[:1], "ORD-1") is False


def test_ticket_close_qualification():
    assert ticket_close_qualification(True, False) == ("refunded", TicketStatus.RESOLVED)
    assert ticket_close_qualification(False, True) == ("rejected", TicketStatus.REJECTED)
    assert ticket_close_qualification(False, False) == (None, None)
