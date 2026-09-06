"""PG 行 ↔ 领域对象保真 codec（无 PG 依赖：纯函数往返）。

覆盖 src/persistence/row_codecs.py 全部 from_row/to_row：订单/工单/操作/政策/订单明细/审计
往返保真。codec 供 PgCommandAdapter / PgCommandService 生产路径使用（读行→领域对象）。
"""
from decimal import Decimal

from src.domain.after_sales import (
    AfterSalesService,
    ApproveCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    PolicyRule,
    RequestType,
    Role,
    SubmitCommand,
)
from src.domain.after_sales.models import AuditEvent
from src.persistence.row_codecs import (
    audit_from_row,
    operation_from_row,
    operation_to_row,
    order_from_row,
    order_item_to_row,
    order_to_row,
    policy_from_row,
    policy_to_row,
    ticket_from_row,
    ticket_to_row,
)
from src.repo import AuditRow
from tests.unit.domain.after_sales.helpers import make_order

POLS = [PolicyRule(policy_id="P-DAMAGED-FULL", tenant_id="T1",
                   request_type=RequestType.REFUND, reason_tags=("damaged",),
                   window_days=30, refund_ratio=Decimal("1.00"))]


def _svc_with_full_flow() -> AfterSalesService:
    svc = AfterSalesService()
    svc.seed_order(make_order(paid="100.00"))
    for p in POLS:
        svc.seed_policy(p)
    t = svc.create_ticket(CreateTicketCommand("T1", "ORD-1", "C1", RequestType.REFUND,
                                              "商品破损", ("damaged",), Role.AGENT, "tk-1"))
    op = svc.create_refund(CreateRefundCommand(t.ticket_id, Decimal("60.00"),
                                               "破损", Role.AGENT, "k-1"))
    svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    op = svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, 1))
    svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM, "success"))
    return svc


# ---------- codec 纯函数往返 ----------

def test_order_row_roundtrip():
    o = make_order(paid="88.50", days_since_sign=7)
    r = order_to_row(o)
    assert r.paid_amount == Decimal("88.50") and r.days_since_sign == 7
    back = order_from_row(r)
    assert back.order_id == o.order_id and back.tenant_id == o.tenant_id
    assert back.paid_amount == Decimal("88.50") and back.status == o.status
    # 单行编解码不含 items（明细由 order_items 表按 (tenant, order) 组装恢复）


def test_ticket_row_roundtrip_with_meta():
    svc = _svc_with_full_flow()
    t = list(svc.export_state()["tickets"].values())[0]
    r = ticket_to_row(t)
    assert r.created_by == "agent" and '"damaged"' in (r.reason_tags or "")
    back = ticket_from_row(r)
    assert back.reason_tags == ("damaged",)
    assert back.created_by == Role.AGENT and back.status == t.status
    assert back.ticket_id == t.ticket_id and back.version == t.version
    assert back.resolution == t.resolution


def test_operation_row_roundtrip():
    svc = _svc_with_full_flow()
    op = list(svc.export_state()["operations"].values())[0]
    r = operation_to_row(op)
    back = operation_from_row(r)
    assert back.operation_id == op.operation_id
    assert back.amount == Decimal("60.00")
    assert back.status == op.status and back.executed is True
    assert back.decision_version == 1 and back.created_by == Role.AGENT


def test_policy_row_roundtrip():
    p = POLS[0]
    r = policy_to_row(p)
    assert r.reason_tags == '["damaged"]'
    assert r.effective_from == "1970-01-01"          # V1 无生效日期语义占位
    back = policy_from_row(r)
    assert back == p and back.reason_tags == ("damaged",)
    assert back.refund_ratio == Decimal("1.00")


def test_order_item_row_roundtrip():
    o = make_order(paid="100.00")
    item = o.items[0]
    r = order_item_to_row(o, item)
    assert r.tenant_id == "T1" and r.order_id == "ORD-1" and r.sku == "SKU-1"
    assert r.quantity == 1 and r.unit_price == Decimal("100.00")


def test_audit_row_roundtrip():
    e = AuditEvent(action="approve", entity_type="operation", entity_id="OP-00002",
                   actor=Role.APPROVER, before="pending_approval", after="approved",
                   idempotency_key="k-1", note=None)
    r = AuditRow("T1", e.action, e.entity_type, e.entity_id, e.actor.value,
                 e.before, e.after, e.idempotency_key, e.note)
    back = audit_from_row(r)
    assert back == e and back.actor == Role.APPROVER
