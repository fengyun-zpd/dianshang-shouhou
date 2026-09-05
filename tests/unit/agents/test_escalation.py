"""证据不足 / 不支持动作 → 转人工；不产生任何草稿与写副作用。"""
import pytest

from src.domain.after_sales import AfterSalesError, AfterSalesErrorCode
from src.domain.after_sales.adapters import MemoryAdapter
from tests.unit.agents.helpers import make_runner
from tests.unit.domain.after_sales.helpers import service_with_policies


def test_order_not_found_escalates_without_side_effects():
    svc, runner = make_runner()
    r = runner.start("T1", "订单 ORD-999 商品破损，要求退款", thread_id="t-es-1")
    assert r.finished is True
    assert r.outcome == "escalated"
    assert r.error_code == AfterSalesErrorCode.ORDER_NOT_FOUND.value
    assert svc.audit_log() == []  # 未建单、无写操作


def test_no_applicable_policy_escalates():
    """政策证据不足（少件无对应政策）→ 转人工，不猜测。"""
    svc, runner = make_runner()
    r = runner.start("T1", "订单 ORD-1 少件，要求退款", thread_id="t-es-2")
    assert r.finished and r.outcome == "escalated"
    assert r.error_code == AfterSalesErrorCode.POLICY_NOT_FOUND.value
    assert svc.audit_log() == []


def test_conflicting_policies_escalate():
    svc = service_with_policies(
        ("P-FULL", ("damaged",), "1.00", 30),
        ("P-HALF", ("damaged",), "0.50", 30),
    )
    from src.agents import WorkflowRunner
    runner = WorkflowRunner(MemoryAdapter(svc))
    r = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="t-es-3")
    assert r.finished and r.outcome == "escalated"
    assert r.error_code == AfterSalesErrorCode.POLICY_CONFLICT.value


def test_unsupported_intent_escalates():
    """换货/补发/升级等 V1 无领域草稿能力 → 转人工，不伪造草稿。"""
    svc, runner = make_runner()
    for i, req in enumerate(("订单 ORD-1 我想换货", "订单 ORD-1 请补发", "订单 ORD-1 给我升级")):
        r = runner.start("T1", req, thread_id=f"t-es-x{i}")
        assert r.finished and r.outcome == "escalated", req
        assert svc.audit_log() == []


def test_unknown_request_escalates():
    svc, runner = make_runner()
    r = runner.start("T1", "你好", thread_id="t-es-4")
    assert r.finished and r.outcome == "escalated"
    assert svc.audit_log() == []
