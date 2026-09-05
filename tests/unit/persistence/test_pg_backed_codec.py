"""PG-backed 会话编解码与保真（无 PG 依赖：memory repo 即可跑）。

覆盖：行 ↔ 领域对象往返、save/load 关键不变量保真、重复请求无副作用（幂等恢复）、
load 后继续审批/关单不丢审计。真实 PostgreSQL 行级验证见 tests/integration。
"""
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesService,
    ApproveCommand,
    CloseTicketCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    PolicyRule,
    RequestType,
    Role,
    SubmitCommand,
)
from src.persistence.pg_backed import (
    operation_from_row,
    operation_to_row,
    order_from_row,
    order_to_row,
    ticket_from_row,
    ticket_to_row,
    PgBackedSession,
)
from src.repo import MemoryAfterSalesRepository
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
    # 订单明细 items 为展示数据、不入表（如实声明的诚实边界）
    assert back.items == []


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


# ---------- save/load 保真与续跑 ----------

def test_save_load_roundtrip_invariants():
    svc = _svc_with_full_flow()
    session = PgBackedSession(MemoryAfterSalesRepository())
    session.save(svc)
    svc2 = session.load(policies=POLS)

    st1, st2 = svc.export_state(), svc2.export_state()
    assert st1["seq"] == st2["seq"] == 2   # create_ticket(1) + create_refund(2)
    assert len(st2["tickets"]) == len(st1["tickets"]) == 1
    assert len(st2["operations"]) == len(st1["operations"]) == 1
    assert len(st2["audit"]) == len(st1["audit"])
    assert st2["refunded"] == st1["refunded"] == {"ORD-1": Decimal("60.00")}
    assert set(st2["idempotency"]) == set(st1["idempotency"])
    op2 = list(st2["operations"].values())[0]
    assert op2.executed is True and op2.decision_version == 1


def test_repeat_request_after_load_no_duplicate_side_effect():
    """重启恢复语义：load 后同幂等键同载荷返回原结果（不重复建单/退款）。"""
    svc = _svc_with_full_flow()
    session = PgBackedSession(MemoryAfterSalesRepository())
    session.save(svc)
    svc2 = session.load(policies=POLS)
    orig = list(svc.export_state()["tickets"].values())[0]

    dup = svc2.create_ticket(CreateTicketCommand("T1", "ORD-1", "C1", RequestType.REFUND,
                                                 "商品破损", ("damaged",), Role.AGENT, "tk-1"))
    assert dup.ticket_id == orig.ticket_id
    assert len(svc2.export_state()["tickets"]) == 1          # 无新增工单
    assert len(svc2.export_state()["operations"]) == 1       # 无新增操作
    assert svc2.refunded_amount("ORD-1") == Decimal("60.00")  # 无重复累计


def test_load_resume_close_and_audit_appends():
    svc = _svc_with_full_flow()
    session = PgBackedSession(MemoryAfterSalesRepository())
    session.save(svc)
    svc2 = session.load(policies=POLS)
    n0 = len(svc2.export_state()["audit"])
    t = list(svc2.export_state()["tickets"].values())[0]
    svc2.close_ticket(CloseTicketCommand(t.ticket_id, Role.AGENT))
    st = svc2.export_state()
    assert len(st["audit"]) > n0                      # 审计在重建实例上继续追加
    assert st["tickets"][t.ticket_id].status.value == "closed"
    # 再 save 仍是完整镜像（关单后的状态）
    session.save(svc2)
    svc3 = session.load(policies=POLS)
    assert svc3.export_state()["tickets"][t.ticket_id].status.value == "closed"
    assert len(svc3.export_state()["audit"]) == len(st["audit"])


def test_save_idem_pointing_to_missing_entity_fails_closed():
    """幂等记录指向不存在实体时 save 拒绝（fail-closed，不静默丢租户）。"""
    svc = AfterSalesService()
    svc.seed_order(make_order())
    svc._idempotency.register("orphan-key", "h", "OP-NOPE")  # 绕过正常流程制造孤儿记录
    session = PgBackedSession(MemoryAfterSalesRepository())
    with pytest.raises(ValueError):
        session.save(svc)
