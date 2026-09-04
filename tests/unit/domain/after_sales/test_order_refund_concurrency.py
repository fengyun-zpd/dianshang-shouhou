"""Task K1：订单级退款并发一致性（RED 先于实现）。

复现的 bug：同一订单实付 100.00，两个不同幂等键并发/顺序各退 60.00，
两个执行都成功 → 累计 120.00 超退。
不变量：无论并发顺序，已执行退款累计 ≤ 实付；超额/冲突返回明确错误码。
"""
import threading
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
from tests.unit.domain.after_sales.helpers import service_with_policies

POL = (("P-DAMAGED-FULL", ("damaged",), "1.00", 30),)


def _service():
    return service_with_policies(*POL)


def _ticket(svc, key="tk-k1"):
    return svc.create_ticket(CreateTicketCommand(
        tenant_id="T1", order_id="ORD-1", customer_id="C1",
        request_type=RequestType.REFUND, reason="商品破损", reason_tags=("damaged",),
        actor=Role.AGENT, idempotency_key=key,
    ))


def _approved_refund(svc, ticket_id: str, amount: str, key: str):
    op = svc.create_refund(CreateRefundCommand(
        ticket_id=ticket_id, amount=Decimal(amount), reason_detail=f"K1-{key}",
        actor=Role.AGENT, idempotency_key=key,
    ))
    op = svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    return svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=op.version))


# ---------- 1) 顺序超退复现：第二个 execute 必须被拒 ----------

def test_sequential_two_executes_must_not_exceed_paid():
    svc = _service()
    ticket = _ticket(svc)
    a = _approved_refund(svc, ticket.ticket_id, "60.00", "k-a")
    b = _approved_refund(svc, ticket.ticket_id, "60.00", "k-b")

    svc.execute(ExecuteCommand(a.operation_id, Role.SYSTEM))
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")

    with pytest.raises(AfterSalesError) as e:
        svc.execute(ExecuteCommand(b.operation_id, Role.SYSTEM))
    assert e.value.code == AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING  # 明确错误码
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")             # 不超退
    assert svc.get_operation(b.operation_id).status == OperationStatus.APPROVED  # 非成功、可再处理


# ---------- 2) 两个已审批操作并发 execute：至多一个成功 ----------

def test_concurrent_execute_two_approved_refunds_one_succeeds():
    svc = _service()
    ticket = _ticket(svc)
    a = _approved_refund(svc, ticket.ticket_id, "60.00", "k-ca")
    b = _approved_refund(svc, ticket.ticket_id, "60.00", "k-cb")

    barrier = threading.Barrier(2)
    ok, errors = [], []
    results: list[str] = []

    def run(op_id: str, idx: int):
        try:
            barrier.wait()
            svc.execute(ExecuteCommand(op_id, Role.SYSTEM))
            ok.append(op_id)
        except AfterSalesError as e:
            errors.append((op_id, e.code))
        finally:
            results.append(op_id)

    t1 = threading.Thread(target=run, args=(a.operation_id, 0))
    t2 = threading.Thread(target=run, args=(b.operation_id, 1))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(ok) == 1                                   # 最多一个成功
    assert len(errors) == 1
    assert errors[0][1] == AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING
    assert svc.refunded_amount("ORD-1") <= Decimal("100.00")
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")


# ---------- 3) unknown 对账与另一退款执行竞争 ----------

def test_unknown_reconcile_vs_execute_competition_stays_within_paid():
    svc = _service()
    ticket = _ticket(svc)
    a = _approved_refund(svc, ticket.ticket_id, "60.00", "k-ua")
    b = _approved_refund(svc, ticket.ticket_id, "60.00", "k-ub")

    # a 执行超时 → UNKNOWN（金额未累计）
    a = svc.execute(ExecuteCommand(a.operation_id, Role.SYSTEM, external_result="timeout"))
    assert a.status == OperationStatus.UNKNOWN
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def run_reconcile():
        try:
            barrier.wait()
            svc.reconcile(ReconcileCommand(a.operation_id, Role.SYSTEM, result="success"))
            outcomes.append(("reconcile", "ok"))
        except AfterSalesError as e:
            outcomes.append(("reconcile", e.code.value))

    def run_execute():
        try:
            barrier.wait()
            svc.execute(ExecuteCommand(b.operation_id, Role.SYSTEM))
            outcomes.append(("execute", "ok"))
        except AfterSalesError as e:
            outcomes.append(("execute", e.code.value))

    t1 = threading.Thread(target=run_reconcile)
    t2 = threading.Thread(target=run_execute)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert svc.refunded_amount("ORD-1") <= Decimal("100.00")
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")      # 恰一个成功累计 60
    oks = [kind for kind, st in outcomes if st == "ok"]
    assert len(oks) == 1                                         # 另一个明确失败
    # 失败方保持可解释状态（不累计、未伪造成功）
    for kind, st in outcomes:
        if st != "ok":
            assert "EXCEEDS_REMAINING" in st
    # 失败的对账操作仍 UNKNOWN（可用原键改 failed 收口）或失败的 execute 仍 APPROVED
    statuses = {o.operation_id: o.status for o in (svc.get_operation(a.operation_id), svc.get_operation(b.operation_id))}
    assert OperationStatus.EXECUTED in statuses.values()
    assert statuses.get(a.operation_id) in (OperationStatus.EXECUTED, OperationStatus.UNKNOWN)


# ---------- 4) unknown 存在时换新键创建被拒 ----------

def test_new_key_rejected_while_unknown_exists():
    svc = _service()
    ticket = _ticket(svc)
    a = _approved_refund(svc, ticket.ticket_id, "60.00", "k-uk")
    svc.execute(ExecuteCommand(a.operation_id, Role.SYSTEM, external_result="timeout"))

    with pytest.raises(AfterSalesError) as e:
        svc.create_refund(CreateRefundCommand(
            ticket_id=ticket.ticket_id, amount=Decimal("30.00"),
            reason_detail="换键重试", actor=Role.AGENT, idempotency_key="k-new",
        ))
    assert e.value.code == AfterSalesErrorCode.OPERATION_UNKNOWN_CONFLICT


# ---------- 5) 同键重复请求：不产生新操作 / 不增退款 / 不增成功审计 ----------

def test_same_key_repeat_no_new_operation_no_extra_refund():
    svc = _service()
    ticket = _ticket(svc)
    first = svc.create_refund(CreateRefundCommand(
        ticket_id=ticket.ticket_id, amount=Decimal("60.00"),
        reason_detail="dup", actor=Role.AGENT, idempotency_key="dup-k1",
    ))
    again = svc.create_refund(CreateRefundCommand(
        ticket_id=ticket.ticket_id, amount=Decimal("60.00"),
        reason_detail="dup", actor=Role.AGENT, idempotency_key="dup-k1",
    ))
    assert again.operation_id == first.operation_id
    assert len(svc.operations_of(ticket.ticket_id)) == 1

    op = svc.submit(SubmitCommand(first.operation_id, Role.AGENT))
    op = svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=op.version))
    svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")
    assert [e.action for e in svc.audit_log()].count("execute") == 1

    # 同键重复（已终态）→ 仍返回原操作，不新增执行
    repeated = svc.create_refund(CreateRefundCommand(
        ticket_id=ticket.ticket_id, amount=Decimal("60.00"),
        reason_detail="dup", actor=Role.AGENT, idempotency_key="dup-k1",
    ))
    assert repeated.operation_id == first.operation_id
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")
    assert [e.action for e in svc.audit_log()].count("execute") == 1


# ---------- 6) 失败/超额执行不增加 refunded ----------

def test_excess_execute_does_not_increase_refunded():
    svc = _service()
    ticket = _ticket(svc)
    # 两笔 60 均先创建并审批（执行前剩余充足，草稿均允许）
    a = _approved_refund(svc, ticket.ticket_id, "60.00", "k-fa")
    b = _approved_refund(svc, ticket.ticket_id, "60.00", "k-fb")
    svc.execute(ExecuteCommand(a.operation_id, Role.SYSTEM))
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")

    before = svc.refunded_amount("ORD-1")
    with pytest.raises(AfterSalesError) as e:
        svc.execute(ExecuteCommand(b.operation_id, Role.SYSTEM))
    assert e.value.code == AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING
    assert svc.refunded_amount("ORD-1") == before                # 不增加
    assert svc.get_operation(b.operation_id).status == OperationStatus.APPROVED


def test_timeout_execute_does_not_increase_refunded():
    svc = _service()
    ticket = _ticket(svc)
    a = _approved_refund(svc, ticket.ticket_id, "60.00", "k-tm")
    svc.execute(ExecuteCommand(a.operation_id, Role.SYSTEM, external_result="timeout"))
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")       # unknown 不累计
