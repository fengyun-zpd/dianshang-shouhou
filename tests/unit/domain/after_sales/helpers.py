"""售后插件测试辅助：固定种子合成订单/政策与快捷建单函数。"""
from decimal import Decimal

from src.domain.after_sales import (
    AfterSalesService,
    CreateTicketCommand,
    Order,
    OrderItem,
    OrderStatus,
    PolicyRule,
    RequestType,
    Role,
)


def make_order(
    order_id: str = "ORD-1",
    tenant_id: str = "T1",
    customer_id: str = "C1",
    paid: str = "100.00",
    days_since_sign: int = 2,
    status: OrderStatus = OrderStatus.DELIVERED,
) -> Order:
    return Order(
        order_id=order_id,
        tenant_id=tenant_id,
        customer_id=customer_id,
        status=status,
        paid_amount=Decimal(paid),
        items=[OrderItem(sku="SKU-1", name="测试商品", quantity=1, unit_price=Decimal(paid))],
        days_since_sign=days_since_sign,
    )


def service_with_policies(
    *policies: tuple[str, tuple[str, ...], str, int],
    order: Order | None = None,
) -> AfterSalesService:
    """构造含订单与多条政策的服务。policies 元素：(policy_id, reason_tags, refund_ratio, window_days)。"""
    svc = AfterSalesService()
    svc.seed_order(order if order is not None else make_order())
    for pid, tags, ratio, window in policies:
        svc.seed_policy(PolicyRule(
            policy_id=pid,
            tenant_id="T1",
            request_type=RequestType.REFUND,
            reason_tags=tags,
            window_days=window,
            refund_ratio=Decimal(ratio),
        ))
    return svc


def baseline_service() -> AfterSalesService:
    """默认基线：破损全额退款（窗口 30 天）。"""
    return service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))


def open_ticket(
    svc: AfterSalesService,
    reason: str = "商品破损",
    reason_tags: tuple[str, ...] = ("damaged",),
    idempotency_key: str = "tk-1",
    actor: Role = Role.AGENT,
) -> object:
    return svc.create_ticket(CreateTicketCommand(
        tenant_id="T1",
        order_id="ORD-1",
        customer_id="C1",
        request_type=RequestType.REFUND,
        reason=reason,
        reason_tags=reason_tags,
        actor=actor,
        idempotency_key=idempotency_key,
    ))
