"""死循环与重复工具调用保护（确定性边界，不依赖模型自觉）。

覆盖：
- 正常工作流 step_count 单调增长；
- 超出最大步数 → 安全停止（AGENT_LOOP_DETECTED / escalated / 零领域写操作）；
- 重复澄清不会无限循环；
- apply_decision 的重复等待不会无限运行；
- 同一工具相同参数不会重复产生副作用（工具去重账本）；
- 审批事实重读（get_operation）永不进入去重账本；
- LangGraph recursion limit 作为第二道防线同样映射为 AGENT_LOOP_DETECTED；
- loop detection 不伪造成功；未知状态不触发换键重试。
"""
from decimal import Decimal

import pytest

from src.agents import AfterSalesGateway, WorkflowRunner
from src.agents.graph import LOOP_ERROR_CODE
from src.agents.tool_ledger import ToolCallLedger, args_digest, tool_call_context
from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    CreateRefundCommand,
    OperationStatus,
    Role,
)
from src.domain.after_sales.adapters import MemoryAdapter
from tests.unit.agents.helpers import REQUEST_DAMAGED, make_runner

REQUEST_NO_ORDER = "我要退款，商品破损"
# 非空但补参仍不足的澄清载荷（空载荷会被 LangGraph 视为无恢复值：不推进任何节点）
CLARIFY_STILL_MISSING = {"description": "商品破损"}


def _runner(svc, max_steps: int) -> WorkflowRunner:
    return WorkflowRunner(MemoryAdapter(svc), max_steps=max_steps)


def _actions(svc) -> list[str]:
    return [e.action for e in svc.audit_log()]


def _executions(svc) -> list[str]:
    return [a for a in _actions(svc) if a.startswith("execute")]


# ---------- 1. step_count 正常增长 ----------

def test_step_count_grows_along_happy_path():
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-steps")
    assert r.waiting_approval is True
    started = r.state["step_count"]
    assert started >= 3, "正常路径必须先经过 parse/gather_evidence/plan/create_ticket_draft"

    mid = runner.get_state("t-steps")
    assert mid.state["step_count"] == started, "只读查询不得推进步数"

    runner.submit_decision(r.state["operation_id"], "approved")
    fin = runner.resume("t-steps")
    assert fin.outcome == "refunded"
    assert fin.state["step_count"] > started, "恢复后继续执行必须继续累加步数"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


# ---------- 2. 超出最大步数 → 安全停止 ----------

def test_max_steps_safe_stop_without_side_effects():
    svc, _ = make_runner()
    runner = _runner(svc, max_steps=8)
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-loop")
    assert r.waiting_approval is True
    audit_before = len(svc.audit_log())

    # 领域审批事实始终未提交：每次 resume 只重读事实并继续等待
    first = runner.resume("t-loop")
    assert first.waiting_approval is True
    assert first.state["decision_still_pending"] is True

    last = runner.resume("t-loop")
    assert last.error_code == LOOP_ERROR_CODE
    assert last.outcome == "escalated"
    assert last.finished is True
    assert last.waiting_approval is False and last.waiting_clarify is False
    assert "转人工" in last.reply

    # 零副作用：未执行退款、未新增任何领域审计
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    assert len(svc.audit_log()) == audit_before
    assert _executions(svc) == []


def test_loop_stop_is_terminal_and_state_view_agrees():
    svc, _ = make_runner()
    runner = _runner(svc, max_steps=8)
    runner.start("T1", REQUEST_DAMAGED, thread_id="t-loop-state")
    runner.resume("t-loop-state")
    stopped = runner.resume("t-loop-state")
    assert stopped.error_code == LOOP_ERROR_CODE

    view = runner.get_state("t-loop-state")
    assert view.finished is True
    assert view.error_code == LOOP_ERROR_CODE
    assert view.outcome == "escalated"
    assert view.waiting_approval is False, "已安全停止的线程不得再对外表现为等待审批"
    assert view.waiting_clarify is False


def test_loop_detection_does_not_fake_success():
    svc, _ = make_runner()
    runner = _runner(svc, max_steps=8)
    runner.start("T1", REQUEST_DAMAGED, thread_id="t-loop-nofake")
    runner.resume("t-loop-nofake")
    r = runner.resume("t-loop-nofake")
    assert r.outcome != "refunded"
    assert r.outcome != "already_executed"
    assert r.error_code == LOOP_ERROR_CODE
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


# ---------- 3. 重复澄清不会无限循环 ----------

def test_repeated_clarify_loop_is_bounded():
    svc, _ = make_runner()
    runner = _runner(svc, max_steps=5)
    r = runner.start("T1", REQUEST_NO_ORDER, thread_id="t-clarify-loop")
    assert r.waiting_clarify is True
    assert r.state.get("operation_id") is None
    audit_before = len(svc.audit_log())

    first = runner.resume("t-clarify-loop", payload=CLARIFY_STILL_MISSING)
    assert first.waiting_clarify is True, "补参仍不足时应继续澄清"

    second = runner.resume("t-clarify-loop", payload=CLARIFY_STILL_MISSING)
    assert second.error_code == LOOP_ERROR_CODE
    assert second.outcome == "escalated"
    assert second.finished is True
    # 澄清循环全程零写操作（未建单、未建草稿、未提交审批）
    assert len(svc.audit_log()) == audit_before
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_empty_clarify_payload_is_a_noop_not_a_loop():
    """空恢复值不推进任何节点（LangGraph 语义）：既不循环，也不产生副作用。"""
    svc, _ = make_runner()
    runner = _runner(svc, max_steps=5)
    r = runner.start("T1", REQUEST_NO_ORDER, thread_id="t-clarify-noop")
    before = r.state.get("step_count")
    for _ in range(3):
        again = runner.resume("t-clarify-noop", payload={})
        assert again.waiting_clarify is True
        assert again.state.get("step_count") == before
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    assert _actions(svc) == []


def test_clarify_loop_is_counted_even_with_repeated_resume():
    svc, _ = make_runner()
    runner = _runner(svc, max_steps=6)
    runner.start("T1", REQUEST_NO_ORDER, thread_id="t-clarify-count")
    counts = []
    for _ in range(2):
        r = runner.resume("t-clarify-count", payload=CLARIFY_STILL_MISSING)
        counts.append(r.state.get("step_count", 0))
    assert counts == sorted(counts) and counts[-1] > counts[0], "澄清循环必须计入 step_count"


# ---------- 3b. 循环终止状态进入持久 checkpoint（进程重启后仍可读） ----------

def test_loop_terminal_state_persisted_for_new_runner(tmp_path):
    """循环安全停止是**持久**终态：新实例（模拟进程重启）仍能读到错误码/转人工原因。"""
    from src.agents.checkpoint import open_sqlite_checkpointer

    db = tmp_path / "loop.sqlite"
    svc, _ = make_runner()
    cp1 = open_sqlite_checkpointer(str(db))
    runner1 = WorkflowRunner(MemoryAdapter(svc), checkpointer=cp1, max_steps=8)
    pending = runner1.start("T1", REQUEST_DAMAGED, thread_id="t-loop-persist")
    assert pending.waiting_approval
    op_id = pending.state["operation_id"]
    runner1.resume("t-loop-persist")
    stopped = runner1.resume("t-loop-persist")
    assert stopped.error_code == LOOP_ERROR_CODE
    audit_after_stop = len(svc.audit_log())

    # “进程重启”：同一 SQLite 文件 + 新运行器实例（无任何内存状态）
    cp2 = open_sqlite_checkpointer(str(db))
    runner2 = WorkflowRunner(MemoryAdapter(svc), checkpointer=cp2, max_steps=8)
    view = runner2.get_state("t-loop-persist", tenant_id="T1")
    assert view.error_code == LOOP_ERROR_CODE, "终态错误码必须在 checkpoint 中持久化"
    assert view.outcome == "escalated"
    assert view.finished is True
    assert view.waiting_approval is False and view.waiting_clarify is False
    assert "转人工" in view.reply
    assert view.state["operation_id"] == op_id, "检测前的草稿/操作上下文应保留"

    # 触发后被拦截的节点不再执行：无新增审计、无退款；检测前的草稿与审批事实保留
    assert len(svc.audit_log()) == audit_after_stop
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    assert svc.get_operation(op_id).status == OperationStatus.PENDING_APPROVAL


def test_loop_stopped_thread_cannot_be_resumed_into_writes(tmp_path):
    """已安全停止的线程即使被继续 resume，也不得执行被拦截节点或产生新写入。

    注意：授权人的领域审批（submit_decision）本身是**领域写**，不属于工作流副作用；
    因此审计基线取「审批之后」，只断言 resume 不再新增任何领域写入。
    """
    svc, _ = make_runner()
    runner = _runner(svc, max_steps=8)
    runner.start("T1", REQUEST_DAMAGED, thread_id="t-loop-block")
    runner.resume("t-loop-block")
    assert runner.resume("t-loop-block").error_code == LOOP_ERROR_CODE

    # 即使事后有人补齐了审批事实，工作流也不得再推进被拦截节点
    op_id = runner.get_state("t-loop-block").state["operation_id"]
    runner.submit_decision(op_id, "approved")
    audit_before_resume = len(svc.audit_log())

    again = runner.resume("t-loop-block")
    assert again.error_code == LOOP_ERROR_CODE
    assert len(svc.audit_log()) == audit_before_resume, "安全停止后不得再新增领域写入"
    assert not any(e.action.startswith("execute") for e in svc.audit_log())
    assert svc.refunded_amount("ORD-1") == Decimal("0.00"), "不得执行退款"


# ---------- 4. 工具去重账本 ----------

def test_same_tool_same_args_in_same_thread_is_deduplicated():
    svc, _ = make_runner()
    ledger = ToolCallLedger()
    gw = AfterSalesGateway(MemoryAdapter(svc), ledger=ledger)
    args = {"order_id": "ORD-1", "customer_id": "C1", "reason": "商品破损",
            "reason_tags": ["damaged"], "idempotency_key": "k-dedup"}

    with tool_call_context("T1", "t-dedup", "create_ticket_draft"):
        first = gw.create_ticket("T1", "ORD-1", "C1", reason="商品破损",
                                 reason_tags=("damaged",), idempotency_key="k-dedup")
        second = gw.create_ticket("T1", "ORD-1", "C1", reason="商品破损",
                                  reason_tags=("damaged",), idempotency_key="k-dedup")

    assert first.ticket_id == second.ticket_id
    assert [e.action for e in svc.audit_log()].count("create_ticket") == 1
    assert ledger.stats()["dedup_hits"] == 1
    # 账本只保存参数摘要，不保存参数原文（避免 PII 落入内存账本）
    assert ledger.key("create_ticket", "T1", "t-dedup", args)[3] == args_digest(args)


def test_dedup_key_is_scoped_by_thread_and_tenant():
    svc, _ = make_runner()
    ledger = ToolCallLedger()
    gw = AfterSalesGateway(MemoryAdapter(svc), ledger=ledger)
    with tool_call_context("T1", "t-a", "create_ticket_draft"):
        gw.create_ticket("T1", "ORD-1", "C1", reason="破损",
                         reason_tags=("damaged",), idempotency_key="k-a")
    with tool_call_context("T1", "t-b", "create_ticket_draft"):
        gw.create_ticket("T1", "ORD-1", "C1", reason="破损",
                         reason_tags=("damaged",), idempotency_key="k-b")
    assert ledger.stats()["dedup_hits"] == 0
    assert [e.action for e in svc.audit_log()].count("create_ticket") == 2


def test_operation_fact_reread_is_never_deduplicated():
    """审批事实重读必须穿透账本：领域决定变化后必须读到最新版本。"""
    svc, runner = make_runner()
    gw = runner.gateway
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-reread")
    op_id = r.state["operation_id"]

    with tool_call_context("T1", "t-reread", "apply_decision"):
        before = gw.get_operation("T1", op_id)
        assert before.status == OperationStatus.PENDING_APPROVAL
        runner.submit_decision(op_id, "approved")          # 领域事实变更
        after = gw.get_operation("T1", op_id)
    assert after.status == OperationStatus.APPROVED, "事实重读被缓存会导致读到过期审批状态"
    assert after.decision_version >= before.decision_version
    assert runner.tool_ledger.stats()["dedup_hits"] == 0


def test_no_tool_context_means_no_dedup():
    """脚本直调网关（无线程上下文）不做跨请求缓存，行为与既有基线一致。"""
    svc, _ = make_runner()
    ledger = ToolCallLedger()
    gw = AfterSalesGateway(MemoryAdapter(svc), ledger=ledger)
    gw.create_ticket("T1", "ORD-1", "C1", reason="破损", reason_tags=("damaged",),
                     idempotency_key="k-noc")
    gw.create_ticket("T1", "ORD-1", "C1", reason="破损", reason_tags=("damaged",),
                     idempotency_key="k-noc2")
    assert ledger.stats()["tool_calls"] == 0


# ---------- 5. recursion limit 第二道防线 ----------

def test_recursion_limit_backstop_maps_to_loop_detected():
    svc, _ = make_runner()
    runner = WorkflowRunner(MemoryAdapter(svc), max_steps=32)
    runner._recursion_limit = 3      # 人为压低：模拟单次 invoke 内超级步失控
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-recursion")
    assert r.error_code == LOOP_ERROR_CODE
    assert r.outcome == "escalated"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    assert _executions(svc) == []


# ---------- 6. 未知状态不换键重试 ----------

def test_unknown_status_never_rekeys_under_loop_pressure():
    """外部结果未知后反复恢复：只能原键对账，绝不自动创建新幂等键。"""
    svc, _ = make_runner()
    runner = _runner(svc, max_steps=8)
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-unknown-loop",
                     simulate_external="timeout")
    op_id = r.state["operation_id"]
    ticket_id = r.state["ticket_id"]
    runner.submit_decision(op_id, "approved")
    done = runner.resume("t-unknown-loop")
    assert done.outcome == "operation_unknown"
    assert done.state["next_action"] == "reconcile_required"
    ops_before = len(svc.operations_of(ticket_id))

    # 继续恢复不会自动换键重试，也不会再次进入执行
    runner.resume("t-unknown-loop")
    runner.resume("t-unknown-loop")
    assert len(svc.operations_of(ticket_id)) == ops_before
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    assert len(_executions(svc)) == 1        # execute_timeout：只发生一次外部执行尝试

    # 原键仍可读且仍为 unknown；换新键在领域层被拒（禁止换键重试）
    assert runner.query_operation(op_id).status == OperationStatus.UNKNOWN
    with pytest.raises(AfterSalesError) as exc:
        svc.create_refund(CreateRefundCommand(
            ticket_id=ticket_id, amount=Decimal("20.00"),
            reason_detail="换键重试", actor=Role.AGENT, idempotency_key="new-key-loop",
        ))
    assert exc.value.code == AfterSalesErrorCode.OPERATION_UNKNOWN_CONFLICT
