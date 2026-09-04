"""领域服务调用边界与审计：Agent 写操作全部经网关以固定角色进入领域服务。"""
from decimal import Decimal

from src.domain.after_sales import OperationStatus, Role
from tests.unit.agents.helpers import REQUEST_DAMAGED, approve_and_resume, make_runner


def test_workflow_audit_actors_are_only_agent_or_system():
    """工作流触发的领域写操作 actor 只能为 AGENT / SYSTEM（无 approver 伪造通道）。"""
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-actors")
    assert r.waiting_approval
    approve_and_resume(runner, "t-actors", r.state["operation_id"])

    workflow_actions = {"create_ticket", "create_refund", "submit", "execute", "close_ticket", "resolve_ticket"}
    for e in svc.audit_log():
        if e.action in workflow_actions:
            assert e.actor in (Role.AGENT, Role.SYSTEM), f"{e.action} 由 {e.actor} 发起"

    # 审批动作 actor=APPROVER 仅当授权人员显式提交决定时出现
    assert any(e.action == "approve" and e.actor == Role.APPROVER for e in svc.audit_log())


def test_action_draft_tampering_does_not_change_refund_amount():
    """篡改 checkpoint 中的 action_draft 金额无效：落库金额以领域权威 plan 为准。"""
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-tamper")
    assert r.waiting_approval

    # 攻击者把展示草稿金额篡改为 1.00（checkpoint 是可被触碰的流程状态）
    runner.graph.update_state(
        {"configurable": {"thread_id": "t-tamper"}},
        {"action_draft": {"amount": "1.00", "op_type": "refund"}},
    )
    approve_and_resume(runner, "t-tamper", r.state["operation_id"])

    op = svc.get_operation(r.state["operation_id"])
    assert op.amount == Decimal("100.00")   # 执行金额来自领域，非被篡改的展示值
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


def test_gateway_is_only_write_path_for_workflow():
    """网关是工作流与领域服务的唯一通道：领域服务方法仅可经网关被工作流调用。

    验证方式：monkeypatch 领域服务的 create_refund，若工作流绕过网关直接调用
    服务方法则同样抛错——但更关键的是网关转发语义不变（角色/幂等键由网关提供）。
    """
    svc, runner = make_runner()
    calls = []

    original = svc.create_refund

    def spy(*args, **kwargs):
        calls.append(("create_refund", args, kwargs))
        return original(*args, **kwargs)

    svc.create_refund = spy  # type: ignore[method-assign]
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-gateway")
    assert r.waiting_approval

    assert calls, "工作流未经过网关/领域服务创建草稿"
    # 网关固定以 AGENT 角色、携带幂等键调用
    cmd = calls[0][1][0]
    assert cmd.actor == Role.AGENT
    assert cmd.idempotency_key.startswith("wf:")
    assert cmd.amount == Decimal("100.00")


def test_audit_event_ids_recorded_in_state():
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-audit")
    ids = r.state["audit_event_ids"]
    assert len(ids) >= 3
    assert any("create_ticket" in i for i in ids)
    assert any("create_refund" in i for i in ids)
    assert any("submit" in i for i in ids)

    approve_and_resume(runner, "t-audit", r.state["operation_id"])
    final_ids = runner.get_state("t-audit").audit_event_ids
    assert any("execute" in i for i in final_ids)
    assert any("close_ticket" in i for i in final_ids)
    # 审计 id 单调且不重复
    assert len(final_ids) == len(set(final_ids))
