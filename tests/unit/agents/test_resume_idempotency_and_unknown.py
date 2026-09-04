"""重复 resume 幂等 / operation_unknown 恢复（仅原 operation_id 查询与对账）。"""
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    CreateRefundCommand,
    OperationStatus,
    Role,
    TicketStatus,
)
from tests.unit.agents.helpers import REQUEST_DAMAGED, approve_and_resume, make_runner


# ---------- 重复 resume 幂等 ----------

def test_double_resume_does_not_repeat_side_effects():
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-double")
    op_id = r.state["operation_id"]

    r2 = approve_and_resume(runner, "t-double", op_id)
    assert r2.outcome == "refunded"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")

    # 第二次 resume：线程已结束 → 不重复执行（退款金额与审计不翻倍）
    r3 = runner.resume("t-double")
    assert r3.finished is True
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    tail = [e.action for e in svc.audit_log()]
    assert tail.count("execute") == 1
    assert tail.count("close_ticket") == 1


def test_repeat_start_same_thread_no_duplicate_draft():
    """同一 thread 重复启动（重复请求）不得重复建单/建草稿/重复退款。"""
    svc, runner = make_runner()
    r1 = runner.start("T1", REQUEST_DAMAGED, thread_id="t-repeat")
    approve_and_resume(runner, "t-repeat", r1.state["operation_id"])
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    ops_before = len(svc.operations_of(r1.state["ticket_id"]))
    audit_before = len(svc.audit_log())

    # 再次以同 thread 发起相同请求：直接返回已处理结果，不新增草稿/副作用
    r2 = runner.start("T1", REQUEST_DAMAGED, thread_id="t-repeat")
    assert r2.finished is True
    assert r2.outcome == "already_executed"
    assert len(svc.operations_of(r1.state["ticket_id"])) == ops_before
    assert len(svc.audit_log()) == audit_before          # 无新增审计（无重复副作用）
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


# ---------- operation_unknown ----------

def test_execute_timeout_enters_unknown_and_reconcile_by_original_operation():
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-unknown", simulate_external="timeout")
    assert r.waiting_approval
    op_id = r.state["operation_id"]
    ticket_id = r.state["ticket_id"]

    # 审批通过后执行 → 外部超时 → UNKNOWN，工单不收尾、金额不累计
    r2 = approve_and_resume(runner, "t-unknown", op_id)
    assert r2.outcome == "operation_unknown"
    assert r2.state["next_action"] == "reconcile_required"
    op = svc.get_operation(op_id)
    assert op.status == OperationStatus.UNKNOWN
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    ticket = svc.get_ticket(ticket_id)
    assert ticket.status == TicketStatus.OPEN  # 结果未知，不擅自关单

    # 只允许用原 operation_id 查询 / 对账
    q = runner.query_operation(op_id)
    assert q.status == OperationStatus.UNKNOWN

    # 换新键重试在领域层被拒（OPERATION_UNKNOWN_CONFLICT）
    with pytest.raises(AfterSalesError) as exc:
        svc.create_refund(CreateRefundCommand(
            ticket_id=ticket_id, amount=Decimal("20.00"),
            reason_detail="换键重试", actor=Role.AGENT, idempotency_key="new-key",
        ))
    assert exc.value.code == AfterSalesErrorCode.OPERATION_UNKNOWN_CONFLICT

    # 原 operation_id 对账成功 → EXECUTED，金额累计
    runner.reconcile_unknown(op_id, "success")
    op2 = svc.get_operation(op_id)
    assert op2.status == OperationStatus.EXECUTED
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")

    # 对账失败路径（终态）—— 用独立服务避免与上面共享退款上限
    svc2, runner2 = make_runner()
    r3 = runner2.start("T1", REQUEST_DAMAGED, thread_id="t-unknown2", simulate_external="timeout")
    op3_id = r3.state["operation_id"]
    approve_and_resume(runner2, "t-unknown2", op3_id)
    runner2.reconcile_unknown(op3_id, "failed")
    assert svc2.get_operation(op3_id).status == OperationStatus.FAILED
    assert svc2.refunded_amount("ORD-1") == Decimal("0.00")  # 失败不累计
