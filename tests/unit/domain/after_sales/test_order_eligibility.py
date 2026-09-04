"""售后插件测试：订单核验、缺参、政策证据与冲突。"""
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    CreateRefundCommand,
    CreateTicketCommand,
    OrderStatus,
    RequestType,
    Role,
)
from tests.unit.domain.after_sales.helpers import (
    baseline_service,
    make_order,
    open_ticket,
    service_with_policies,
)


def _create_ticket(svc, *, order_id="ORD-1", tenant_id="T1", reason="商品破损",
                   tags=("damaged",), key="tk-x", actor=Role.AGENT):
    return svc.create_ticket(CreateTicketCommand(
        tenant_id=tenant_id, order_id=order_id, customer_id="C1",
        request_type=RequestType.REFUND, reason=reason,
        reason_tags=tags, actor=actor, idempotency_key=key,
    ))


# ---------- 订单核验 ----------

def test_order_not_found():
    svc = baseline_service()
    with pytest.raises(AfterSalesError) as e:
        _create_ticket(svc, order_id="ORD-NOPE")
    assert e.value.code == AfterSalesErrorCode.ORDER_NOT_FOUND


def test_tenant_mismatch_rejected():
    svc = baseline_service()
    with pytest.raises(AfterSalesError) as e:
        _create_ticket(svc, tenant_id="T2")  # 订单归属 T1
    assert e.value.code == AfterSalesErrorCode.TENANT_MISMATCH


def test_closed_order_not_eligible():
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30),
                                order=make_order(status=OrderStatus.CLOSED))
    with pytest.raises(AfterSalesError) as e:
        _create_ticket(svc)
    assert e.value.code == AfterSalesErrorCode.ORDER_STATUS_NOT_ELIGIBLE


# ---------- 缺参 ----------

def test_missing_reason_rejected():
    svc = baseline_service()
    with pytest.raises(AfterSalesError) as e:
        _create_ticket(svc, reason="   ")
    assert e.value.code == AfterSalesErrorCode.MISSING_REQUIRED_FIELD


def test_missing_reason_tags_rejected():
    svc = baseline_service()
    with pytest.raises(AfterSalesError) as e:
        _create_ticket(svc, tags=())
    assert e.value.code == AfterSalesErrorCode.MISSING_REQUIRED_FIELD


# ---------- 政策证据 ----------

def test_no_applicable_policy_escalates():
    """无适用政策 = 证据不足 → 明确错误（转人工语义），不猜测。"""
    svc = baseline_service()
    with pytest.raises(AfterSalesError) as e:
        _create_ticket(svc, tags=("missing_item",))  # 无政策命中
    assert e.value.code == AfterSalesErrorCode.POLICY_NOT_FOUND


def test_outside_policy_window_no_match():
    """签收超过政策窗口 → 政策不适用。"""
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30),
                                order=make_order(days_since_sign=45))
    with pytest.raises(AfterSalesError) as e:
        _create_ticket(svc)
    assert e.value.code == AfterSalesErrorCode.POLICY_NOT_FOUND


def test_conflicting_policies_escalate():
    """冲突政策（破损全额 vs 破损半额）→ 显式转人工。"""
    svc = service_with_policies(
        ("P-FULL", ("damaged",), "1.00", 30),
        ("P-HALF", ("damaged",), "0.50", 30),
    )
    with pytest.raises(AfterSalesError) as e:
        _create_ticket(svc)
    assert e.value.code == AfterSalesErrorCode.POLICY_CONFLICT


# ---------- 金额参数 ----------

def test_float_amount_rejected():
    """金额禁止 float：parse_money 抛出 ValueError。"""
    svc = baseline_service()
    ticket = open_ticket(svc)
    with pytest.raises(ValueError):
        svc.create_refund(CreateRefundCommand(
            ticket_id=ticket.ticket_id,
            amount=50.5,  # float
            reason_detail="破损",
            actor=Role.AGENT,
            idempotency_key="k-float",
        ))


def test_non_positive_amount_rejected():
    svc = baseline_service()
    ticket = open_ticket(svc)
    for bad in ("0.00", "-5.00"):
        with pytest.raises(AfterSalesError) as e:
            svc.create_refund(CreateRefundCommand(
                ticket_id=ticket.ticket_id, amount=Decimal(bad),
                reason_detail="破损", actor=Role.AGENT, idempotency_key=f"k-{bad}",
            ))
        assert e.value.code == AfterSalesErrorCode.AMOUNT_NOT_POSITIVE
