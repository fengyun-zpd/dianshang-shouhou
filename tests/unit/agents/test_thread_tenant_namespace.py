"""工作流 checkpoint 的租户命名空间回归测试。"""
import pytest
from langgraph.checkpoint.memory import MemorySaver

from src.agents import WorkflowRunner
from tests.unit.agents.helpers import REQUEST_DAMAGED, make_runner


def test_same_thread_name_uses_distinct_tenant_checkpoint_namespace():
    svc, _ = make_runner()
    checkpointer = MemorySaver()
    runner = WorkflowRunner(svc, checkpointer=checkpointer)
    runner.start("T1", REQUEST_DAMAGED, thread_id="shared-thread")
    assert runner._cfg("shared-thread")["configurable"]["thread_id"] == "T1:shared-thread"

    with pytest.raises(ValueError, match="THREAD_TENANT_CONFLICT"):
        runner.start("T2", REQUEST_DAMAGED, thread_id="shared-thread")

    other = WorkflowRunner(svc, checkpointer=checkpointer)
    other.start("T2", "你好", thread_id="shared-thread")
    assert other._cfg("shared-thread")["configurable"]["thread_id"] == "T2:shared-thread"


def test_resume_on_new_runner_requires_explicit_tenant_binding():
    svc, runner = make_runner()
    checkpointer = MemorySaver()
    runner = WorkflowRunner(svc, checkpointer=checkpointer)
    pending = runner.start("T1", REQUEST_DAMAGED, thread_id="recover-thread")
    assert pending.waiting_approval

    resumed = WorkflowRunner(svc, checkpointer=checkpointer)
    with pytest.raises(ValueError, match="UNKNOWN_THREAD"):
        resumed.resume("recover-thread")

    resumed.submit_decision(pending.state["operation_id"], "approved")
    final = resumed.resume("recover-thread", tenant_id="T1")
    assert final.finished
    assert final.outcome == "refunded"
