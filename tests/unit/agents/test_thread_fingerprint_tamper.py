"""K3：伪造 checkpoint 请求指纹篡改 → 同 thread 拒绝且零副作用。"""
import pytest

from src.agents import WorkflowRunner
from src.agents.runner import ThreadConflictError
from tests.unit.agents.helpers import REQUEST_DAMAGED, make_runner


def _finish(runner, thread, op_id):
    runner.submit_decision(op_id, "approved")
    return runner.resume(thread)


def test_forged_fingerprint_in_checkpoint_rejected_on_resume_start():
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="fp-1")
    _finish(runner, "fp-1", r.state["operation_id"])

    # 攻击者篡改 checkpoint 中的请求指纹（update_state 直达 checkpoint）
    runner.graph.update_state(
        runner._cfg("fp-1", "T1"),
        {"thread_request_fingerprint": {"tenant_id": "T1", "request_hash": "forged"}},
    )
    with pytest.raises(ThreadConflictError):
        runner.start("T1", REQUEST_DAMAGED, thread_id="fp-1")
    assert svc.refunded_amount("ORD-1").__str__() == "100.00"   # 无新增副作用
    assert len(svc.audit_log()) >= 3                            # 原流程审计保留，未被追加


def test_forged_fingerprint_rejected_even_different_request_text():
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="fp-2")
    assert r.waiting_approval
    runner.graph.update_state(
        runner._cfg("fp-2", "T1"),
        {"thread_request_fingerprint": {"tenant_id": "T1", "request_hash": "evil"}},
    )
    with pytest.raises(ThreadConflictError):
        runner.start("T1", REQUEST_DAMAGED, thread_id="fp-2", order_id_hint="ORD-999")
