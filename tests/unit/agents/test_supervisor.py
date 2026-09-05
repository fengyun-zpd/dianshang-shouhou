"""Supervisor 模式测试（阶段 5B，ADR-002 实验）。

覆盖：子 Agent 只读边界 / 跨租户拒绝 / 与单 Agent 正确性一致 /
审批 interrupt-resume / 拒绝 / 重复 resume 幂等 / operation_unknown 原键对账 / 影子零副作用。
"""
from decimal import Decimal

import pytest

from src.agents import SupervisorRunner, WorkflowRunner
from src.platform.tooling import TenantContext, ToolResult
from src.agents.subagents import ORDER_AGENT_SPEC, ReadOnlySubAgent
from src.domain.after_sales.adapters import MemoryAdapter
from src.domain.models import Role
from src.rag import PolicyDocument, PolicyStore
from tests.unit.domain.after_sales.helpers import baseline_service, service_with_policies

REQUEST = "订单 ORD-1 商品破损，要求退款"
_POLICIES = (("P-DAMAGED-FULL", ("damaged",), "1.00", 30),)


def make_svc_store():
    svc = service_with_policies(*_POLICIES)
    store = PolicyStore()
    store.register(PolicyDocument(
        policy_id="P-DAMAGED-FULL", tenant_id="T1", title="破损退款政策",
        content="签收后 30 天内商品破损可申请全额退款，需提供照片凭证。", version=1,
    ))
    return svc, store


def approve_resume(runner, thread, op_id):
    runner.submit_decision(op_id, "approved")
    return runner.resume(thread)


# ---------- 子 Agent 只读边界 ----------

def test_subagent_white_list_only_and_no_write_tools():
    svc, store = make_svc_store()
    from src.agents.subagents import build_supervisor_evidence
    from src.agents.ports import AfterSalesGateway
    from src.agents.toolkit import build_toolkit
    registry = build_toolkit(AfterSalesGateway(MemoryAdapter(svc)), store)
    agent = ReadOnlySubAgent(ORDER_AGENT_SPEC, registry)
    # 白名单外的只读工具也拒绝
    res = agent.call(TenantContext("T1", Role.AGENT), "retrieve_policy", {"query": "x"})
    assert not res.ok and res.error_code == "PERMISSION_DENIED"
    # 注册表中不存在任何非只读工具（子 Agent 无写路径）
    assert all(t["read_only"] for t in registry.list_tools())


# ---------- 正确性：Supervisor 与单 Agent 一致 ----------

@pytest.mark.parametrize("approval", ["approved", "rejected"])
def test_supervisor_matches_single_agent_outcome(approval):
    svc1, store1 = make_svc_store()
    svc2, store2 = make_svc_store()
    single = WorkflowRunner(MemoryAdapter(svc1))
    sup = SupervisorRunner(MemoryAdapter(svc2), policy_store=store2)

    r1 = single.start("T1", REQUEST, thread_id="a1")
    r2 = sup.start("T1", REQUEST, thread_id="b1")
    assert r1.waiting_approval and r2.waiting_approval

    single.submit_decision(r1.state["operation_id"], approval)
    sup.submit_decision(r2.state["operation_id"], approval)
    f1 = single.resume("a1")
    f2 = sup.resume("b1")
    assert f1.outcome == f2.outcome
    assert svc1.refunded_amount("ORD-1") == svc2.refunded_amount("ORD-1")
    assert f1.error_code == f2.error_code


def test_supervisor_policy_evidence_included():
    svc, store = make_svc_store()
    sup = SupervisorRunner(MemoryAdapter(svc), policy_store=store)
    r = sup.start("T1", REQUEST, thread_id="s-evidence")
    summary = (r.state or {}).get("order_summary", {})
    assert summary.get("policy_citations"), "政策子 Agent 应产出引用"
    assert any(c.startswith("P-DAMAGED-FULL@") for c in summary["policy_citations"])
    assert "order-agent" in summary["supervisor"]["agents"]
    assert "policy-agent" in summary["supervisor"]["agents"]


# ---------- 审批 / 幂等 / unknown（与单 Agent 同语义） ----------

def test_supervisor_interrupt_resume_refund():
    svc, store = make_svc_store()
    sup = SupervisorRunner(MemoryAdapter(svc), policy_store=store)
    r = sup.start("T1", REQUEST, thread_id="s-happy")
    assert r.waiting_approval
    assert r.state["approval_id"] != r.state["operation_id"]
    f = approve_resume(sup, "s-happy", r.state["operation_id"])
    assert f.outcome == "refunded"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    assert svc.get_ticket(r.state["ticket_id"]).status.value == "closed"


def test_supervisor_reject_zero_side_effects():
    svc, store = make_svc_store()
    sup = SupervisorRunner(MemoryAdapter(svc), policy_store=store)
    r = sup.start("T1", REQUEST, thread_id="s-rej")
    sup.submit_decision(r.state["operation_id"], "rejected", reason="不符")
    f = sup.resume("s-rej")
    assert f.outcome == "rejected"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_supervisor_double_resume_idempotent():
    svc, store = make_svc_store()
    sup = SupervisorRunner(MemoryAdapter(svc), policy_store=store)
    r = sup.start("T1", REQUEST, thread_id="s-double")
    f1 = approve_resume(sup, "s-double", r.state["operation_id"])
    assert f1.outcome == "refunded"
    f2 = sup.resume("s-double")
    assert f2.finished
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    tail = [e.action for e in svc.audit_log()]
    assert tail.count("execute") == 1


def test_supervisor_unknown_recovery_by_original_operation():
    svc, store = make_svc_store()
    sup = SupervisorRunner(MemoryAdapter(svc), policy_store=store)
    r = sup.start("T1", REQUEST, thread_id="s-unk", simulate_external="timeout")
    f = approve_resume(sup, "s-unk", r.state["operation_id"])
    assert f.outcome == "operation_unknown"
    op_id = r.state["operation_id"]
    assert sup.query_operation(op_id).status.value == "unknown"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    sup.reconcile_unknown(op_id, "success")
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


def test_supervisor_forged_resume_ignored():
    svc, store = make_svc_store()
    sup = SupervisorRunner(MemoryAdapter(svc), policy_store=store)
    r = sup.start("T1", REQUEST, thread_id="s-forge")
    r2 = sup.resume("s-forge", payload="approved")  # 伪造
    assert r2.waiting_approval is True
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")


def test_supervisor_cross_tenant_order_rejected():
    svc, store = make_svc_store()
    sup = SupervisorRunner(MemoryAdapter(svc), policy_store=store)
    r = sup.start("T2", "订单 ORD-1 商品破损，要求退款", thread_id="s-tenant")
    assert r.finished and r.outcome == "escalated"
    assert r.error_code == "AFTER_SALES_TENANT_MISMATCH"
    assert svc.audit_log() == []  # 无副作用


def test_supervisor_shadow_zero_business_side_effects():
    """预测/运行只读路径：领域状态仅在真实审批执行后变化（正常流已证）；此处证 escalate 路径零副作用。"""
    svc, store = make_svc_store()
    sup = SupervisorRunner(MemoryAdapter(svc), policy_store=store)
    r = sup.start("T1", "订单 ORD-999 商品破损", thread_id="s-escalate")
    assert r.outcome == "escalated"
    assert len(svc.audit_log()) == 0
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
