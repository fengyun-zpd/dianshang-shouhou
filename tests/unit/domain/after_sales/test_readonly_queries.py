"""售后插件只读查询与确定性退款计划测试（阶段 2 Agent 编排依赖的最小领域扩展）。"""
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    CreateTicketCommand,
    OrderStatus,
    RequestType,
    Role,
)
from src.domain.after_sales.models import RefundPlan
from tests.unit.domain.after_sales.helpers import (
    baseline_service,
    make_order,
    open_ticket,
    service_with_policies,
)


def _open_second_customer_ticket(svc) -> None:
    svc.create_ticket(CreateTicketCommand(
        tenant_id="T1", order_id="ORD-1", customer_id="C2",
        request_type=RequestType.REFUND, reason="少件", reason_tags=("missing_item",),
        actor=Role.AGENT, idempotency_key="tk-c2",
    ))


# ---------- 只读订单查询 ----------

def test_get_order_by_id_ok():
    svc = baseline_service()
    order = svc.get_order_by_id("T1", "ORD-1")
    assert order.order_id == "ORD-1"
    assert order.paid_amount == Decimal("100.00")


def test_get_order_by_id_tenant_mismatch():
    svc = baseline_service()
    with pytest.raises(AfterSalesError) as e:
        svc.get_order_by_id("T2", "ORD-1")
    assert e.value.code == AfterSalesErrorCode.TENANT_MISMATCH


def test_get_order_by_id_not_found():
    svc = baseline_service()
    with pytest.raises(AfterSalesError) as e:
        svc.get_order_by_id("T1", "ORD-NOPE")
    assert e.value.code == AfterSalesErrorCode.ORDER_NOT_FOUND


# ---------- 只读历史工单 ----------

def test_list_customer_tickets_scoped_to_customer_and_tenant():
    svc = service_with_policies(
        ("P-DAMAGED-FULL", ("damaged",), "1.00", 30),
        ("P-MISSING-FULL", ("missing_item",), "1.00", 30),
    )
    open_ticket(svc, idempotency_key="tk-1")      # C1
    _open_second_customer_ticket(svc)             # C2

    assert [t.customer_id for t in svc.list_customer_tickets("T1", "C1")] == ["C1"]
    assert [t.customer_id for t in svc.list_customer_tickets("T1", "C2")] == ["C2"]
    # 跨租户视角不返回任何工单
    assert svc.list_customer_tickets("T2", "C1") == []


# ---------- 确定性退款计划 ----------

def test_compute_refund_plan_full_ratio():
    svc = baseline_service()  # 破损全额 (ratio 1.00, 实付 100.00)
    plan = svc.compute_refund_plan("T1", "ORD-1", RequestType.REFUND, ("damaged",))
    assert isinstance(plan, RefundPlan)
    assert plan.amount == Decimal("100.00")
    assert plan.refund_ratio == Decimal("1.00")
    assert plan.policy_id == "P-DAMAGED-FULL"


def test_compute_refund_plan_half_ratio():
    svc = service_with_policies(("P-HALF", ("damaged",), "0.50", 30))
    plan = svc.compute_refund_plan("T1", "ORD-1", RequestType.REFUND, ("damaged",))
    assert plan.amount == Decimal("50.00")


def test_compute_refund_plan_no_policy():
    svc = baseline_service()
    with pytest.raises(AfterSalesError) as e:
        svc.compute_refund_plan("T1", "ORD-1", RequestType.REFUND, ("missing_item",))
    assert e.value.code == AfterSalesErrorCode.POLICY_NOT_FOUND


def test_compute_refund_plan_conflict():
    svc = service_with_policies(
        ("P-FULL", ("damaged",), "1.00", 30),
        ("P-HALF", ("damaged",), "0.50", 30),
    )
    with pytest.raises(AfterSalesError) as e:
        svc.compute_refund_plan("T1", "ORD-1", RequestType.REFUND, ("damaged",))
    assert e.value.code == AfterSalesErrorCode.POLICY_CONFLICT


def test_compute_refund_plan_closed_order():
    svc = service_with_policies(
        ("P-DAMAGED-FULL", ("damaged",), "1.00", 30),
        order=make_order(status=OrderStatus.CLOSED),
    )
    with pytest.raises(AfterSalesError) as e:
        svc.compute_refund_plan("T1", "ORD-1", RequestType.REFUND, ("damaged",))
    assert e.value.code == AfterSalesErrorCode.ORDER_STATUS_NOT_ELIGIBLE
