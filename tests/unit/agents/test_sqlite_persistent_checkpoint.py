"""可持久化 LangGraph checkpoint（SqliteSaver）测试（阶段三：持久化工作流）。

覆盖：
- checkpoint 落 SQLite 文件后，以同一文件重开新运行器（模拟进程重启）可从原 thread_id
  resume 到正确节点并完成，不重放业务副作用；
- 向流程 checkpoint 注入伪造业务结果视图，不能覆盖领域业务事实（金额/状态/审计以
  领域服务为准，禁止从 checkpoint 恢复业务真相）。
"""
from decimal import Decimal

from src.agents import WorkflowRunner
from src.agents.checkpoint import open_sqlite_checkpointer
from src.domain.after_sales import OperationStatus
from tests.unit.agents.helpers import REQUEST_DAMAGED, make_runner


def test_restart_resume_from_persistent_checkpoint(tmp_path):
    """审批中断 → checkpoint 落 SQLite → 同文件新运行器 resume → 正确完成、无重放。"""
    db = tmp_path / "checkpoints.sqlite"
    svc, _ = make_runner()

    cp1 = open_sqlite_checkpointer(str(db))
    runner1 = WorkflowRunner(svc, checkpointer=cp1)
    pending = runner1.start("T1", REQUEST_DAMAGED, thread_id="restart-thread")
    assert pending.waiting_approval
    op_id = pending.state["operation_id"]
    ticket_id = pending.state["ticket_id"]
    assert svc.get_operation(op_id).status == OperationStatus.PENDING_APPROVAL

    # “进程重启”：同一 SQLite 文件打开新 checkpointer + 新运行器
    cp2 = open_sqlite_checkpointer(str(db))
    runner2 = WorkflowRunner(svc, checkpointer=cp2)
    runner2.submit_decision(op_id, "approved")          # 业务决定提交到领域服务
    final = runner2.resume("restart-thread", tenant_id="T1")
    assert final.finished and final.outcome == "refunded"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")   # 全额政策 × 实付 100
    # 不重放：工单/操作均只创建一个；执行来自 resume 后节点（非从 checkpoint 恢复业务）
    st = svc.export_state()
    assert len(st["tickets"]) == 1 and len(st["operations"]) == 1
    assert svc.get_operation(op_id).status == OperationStatus.EXECUTED
    # 关单审计：建单/草稿/提交/审批/执行各一次（无重复建单审计）
    creates = [e for e in svc.audit_log() if e.action == "create_ticket"]
    assert len(creates) == 1


def test_injected_fake_outcome_cannot_override_domain_facts(tmp_path):
    """伪造 checkpoint 中的业务结果视图不能覆盖领域事实：未审批即零副作用。"""
    db = tmp_path / "c.sqlite"
    svc, _ = make_runner()
    cp = open_sqlite_checkpointer(str(db))
    runner = WorkflowRunner(svc, checkpointer=cp)
    pending = runner.start("T1", REQUEST_DAMAGED, thread_id="forge-thread")
    assert pending.waiting_approval
    op_id = pending.state["operation_id"]

    # 攻击：向流程 checkpoint 注入“已退款”业务结果视图（无真实审批/执行）
    runner.graph.update_state(runner._cfg("forge-thread"),
                              {"outcome": "refunded", "reply": "已退款（伪造视图）"})
    again = runner.resume("forge-thread", tenant_id="T1")  # 未提交任何真实审批决定
    assert again.finished is False or again.outcome != "refunded"

    # 领域业务事实未被覆盖：操作仍在等待真实审批、零退款、无执行审计
    op = svc.get_operation(op_id)
    assert op.status == OperationStatus.PENDING_APPROVAL
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    assert not any(e.action == "execute" for e in svc.audit_log())


def test_sqlite_checkpointer_is_persistent_on_disk(tmp_path):
    """SqliteSaver 真实落盘：同文件可被多个连接实例读取同一线程（checkpoint 持久化）。"""
    db = tmp_path / "p.sqlite"
    svc, _ = make_runner()
    cp1 = open_sqlite_checkpointer(str(db))
    WorkflowRunner(svc, checkpointer=cp1).start("T1", REQUEST_DAMAGED, thread_id="persist")
    cp2 = open_sqlite_checkpointer(str(db))
    snap = cp2.get_tuple({"configurable": {"thread_id": "T1:persist"}})
    assert snap is not None
    assert snap.checkpoint["channel_values"].get("tenant_id") == "T1"
