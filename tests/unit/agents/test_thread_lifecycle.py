"""线程生命周期与请求指纹（任务卡 J）：同一 thread_id 只能继续原请求。

- 已结束线程提交不同请求 → 拒绝；
- 挂起线程提交不同请求 → 拒绝（禁止新 order 与旧 ticket 混合）；
- 同一请求重复提交 → 返回原结果（不重放、不产生新副作用）；
- 租户绑定不可变（THREAD_TENANT_CONFLICT）。
"""
from decimal import Decimal

import pytest

from src.agents import WorkflowRunner
from src.agents.runner import ThreadConflictError, UnknownThreadError
from tests.unit.agents.helpers import REQUEST_DAMAGED, make_runner


def _finish_refund(runner: WorkflowRunner, thread_id: str, operation_id: str):
    runner.submit_decision(operation_id, "approved")
    return runner.resume(thread_id)


def test_finished_thread_rejects_different_request():
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-j1")
    _finish_refund(runner, "t-j1", r.state["operation_id"])
    assert runner.get_state("t-j1").finished

    with pytest.raises(ThreadConflictError):
        runner.start("T1", "订单 ORD-1 少件，要求退款", thread_id="t-j1")
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")  # 拒绝未改变已执行退款


def test_pending_thread_rejects_different_request_before_mixing():
    """挂起线程提交不同请求必须拒绝（否则新 order 会与旧 ticket 混合）。"""
    svc, runner = make_runner()
    r1 = runner.start("T1", REQUEST_DAMAGED, thread_id="t-j2")
    assert r1.waiting_approval
    before = len(svc.audit_log())

    with pytest.raises(ThreadConflictError):
        runner.start("T1", REQUEST_DAMAGED, thread_id="t-j2", order_id_hint="ORD-999")
    # 无副作用：未新增任何审计 / 工单
    assert len(svc.audit_log()) == before


def test_repeat_same_request_returns_original_result():
    svc, runner = make_runner()
    r1 = runner.start("T1", REQUEST_DAMAGED, thread_id="t-j4")
    fin1 = _finish_refund(runner, "t-j4", r1.state["operation_id"])
    assert fin1.outcome == "refunded"
    before = len(svc.audit_log())

    r2 = runner.start("T1", REQUEST_DAMAGED, thread_id="t-j4")
    assert r2.finished is True
    assert r2.outcome == "refunded"                       # 返回原结果
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    assert len(svc.audit_log()) == before                 # 无重复副作用


def test_thread_scope_is_tenant_qualified():
    """线程作用域 = (tenant_id, thread_id)：同名线程跨租户互不冲突，但不可跨租户读写。"""
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-j5")
    assert r.state["tenant_id"] == "T1"

    # 同名线程在 T2 下是独立线程（不冲突），checkpoint 键空间相互隔离
    t2 = runner.start("T2", REQUEST_DAMAGED, thread_id="t-j5")
    assert t2.state["tenant_id"] == "T2"
    assert (runner._cfg("t-j5", "T1")["configurable"]["thread_id"]
            != runner._cfg("t-j5", "T2")["configurable"]["thread_id"])

    # 跨租户读写一律 UnknownThreadError（通用 404，不暴露所属租户）
    with pytest.raises(UnknownThreadError) as wrong_tenant:
        runner.resume("t-j5", tenant_id="T3")
    assert "T1" not in str(wrong_tenant.value) and "T2" not in str(wrong_tenant.value)
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_request_fingerprint_recorded_in_state():
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-j6")
    fp = (r.state or {}).get("thread_request_fingerprint")
    assert fp is not None
    assert fp["tenant_id"] == "T1"
    assert fp["request_hash"]
