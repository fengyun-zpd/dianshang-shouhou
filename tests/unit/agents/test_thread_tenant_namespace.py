"""工作流 checkpoint 的租户命名空间与线程作用域回归测试。

线程唯一键 = (tenant_id, thread_id)：
- 同名线程在不同租户下互不冲突（checkpoint 键空间 `tenant:thread`）；
- 错误租户与不存在的线程返回同一个 UnknownThreadError（通用 404，不泄露归属）。
"""
import pytest
from langgraph.checkpoint.memory import MemorySaver

from src.agents import UnknownThreadError, WorkflowRunner
from src.domain.after_sales.adapters import MemoryAdapter
from tests.unit.agents.helpers import REQUEST_DAMAGED, make_runner


def test_same_thread_name_uses_distinct_tenant_checkpoint_namespace():
    svc, _ = make_runner()
    checkpointer = MemorySaver()
    runner = WorkflowRunner(MemoryAdapter(svc), checkpointer=checkpointer)
    runner.start("T1", REQUEST_DAMAGED, thread_id="shared-thread")
    assert runner._cfg("shared-thread", "T1")["configurable"]["thread_id"] == "T1:shared-thread"

    # 同名线程在 T2 下是**另一个线程**，不冲突（各自命名空间）
    t2 = runner.start("T2", "你好", thread_id="shared-thread")
    assert t2.state["tenant_id"] == "T2"
    assert runner._cfg("shared-thread", "T2")["configurable"]["thread_id"] == "T2:shared-thread"

    other = WorkflowRunner(MemoryAdapter(svc), checkpointer=checkpointer)
    other.start("T2", "你好", thread_id="shared-thread")
    assert other._cfg("shared-thread", "T2")["configurable"]["thread_id"] == "T2:shared-thread"


def test_wrong_tenant_lookup_is_generic_unknown_thread():
    """错误租户与不存在线程的错误消息结构一致（除线程名外），不泄露所属租户。"""
    svc, _ = make_runner()
    checkpointer = MemorySaver()
    runner = WorkflowRunner(MemoryAdapter(svc), checkpointer=checkpointer)
    runner.start("T1", REQUEST_DAMAGED, thread_id="t1-only")

    with pytest.raises(UnknownThreadError) as wrong_tenant:
        runner.get_state("t1-only", tenant_id="T2")
    with pytest.raises(UnknownThreadError) as missing:
        runner.get_state("never-existed", tenant_id="T2")

    assert "T1" not in str(wrong_tenant.value), "错误消息不得包含所属租户"
    assert str(wrong_tenant.value).replace("t1-only", "X") == \
        str(missing.value).replace("never-existed", "X")

    with pytest.raises(UnknownThreadError):
        runner.resume("t1-only", tenant_id="T2")

    # 新实例（无进程内缓存）未显式给租户 → 同样 404，绝不猜测租户
    fresh = WorkflowRunner(MemoryAdapter(svc), checkpointer=checkpointer)
    with pytest.raises(UnknownThreadError):
        fresh.get_state("t1-only")
    with pytest.raises(UnknownThreadError):
        fresh.resume("t1-only")


def test_resume_on_new_runner_requires_explicit_tenant_binding():
    svc, _ = make_runner()
    checkpointer = MemorySaver()
    runner = WorkflowRunner(MemoryAdapter(svc), checkpointer=checkpointer)
    pending = runner.start("T1", REQUEST_DAMAGED, thread_id="recover-thread")
    assert pending.waiting_approval

    resumed = WorkflowRunner(MemoryAdapter(svc), checkpointer=checkpointer)
    with pytest.raises(UnknownThreadError):
        resumed.resume("recover-thread")
    with pytest.raises(UnknownThreadError):
        resumed.resume("recover-thread", tenant_id="T2")   # 错误租户同样不可读

    # 显式给出正确租户时，新实例按 checkpoint 事实恢复（内存 profile 仅限本机同文件）
    resumed.submit_decision(pending.state["operation_id"], "approved", tenant_id="T1")
    final = resumed.resume("recover-thread", tenant_id="T1")
    assert final.finished
    assert final.outcome == "refunded"
