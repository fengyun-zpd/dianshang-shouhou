"""Agent 工作流状态 Schema 测试。"""
from src.agents import WorkflowRunner
from src.agents.state import AgentState, new_state

from tests.unit.agents.helpers import REQUEST_DAMAGED, make_runner

REQUIRED_FIELDS = {
    "tenant_id", "thread_id", "ticket_id", "intent", "missing_fields",
    "evidence_refs", "action_draft", "risk_level", "approval_id",
    "operation_id", "next_action", "error_code", "audit_event_ids",
}


def test_schema_declares_all_required_fields():
    declared = set(AgentState.__annotations__.keys())
    assert REQUIRED_FIELDS <= declared, f"缺少字段：{REQUIRED_FIELDS - declared}"


def test_new_state_defaults():
    s = new_state()
    assert s["missing_fields"] == []
    assert s["evidence_refs"] == []
    assert s["audit_event_ids"] == []
    assert s["reason_tags"] == []
    assert s["simulate_external"] == "success"


def test_interrupted_state_roundtrip_through_checkpoint():
    """审批中断后，checkpoint 恢复的状态含全部关键字段。"""
    svc, runner = make_runner()
    r = runner.start("T1", REQUEST_DAMAGED, thread_id="t-schema")
    assert r.waiting_approval

    snap = runner.get_state("t-schema")
    s = snap.state or {}
    assert s["tenant_id"] == "T1"
    assert s["thread_id"] == "t-schema"
    assert s["intent"] == "refund"
    assert s["ticket_id"] and s["ticket_id"].startswith("TKT-")
    assert s["operation_id"] and s["operation_id"].startswith("OP-")
    assert s["approval_id"] and s["approval_id"].startswith("APR-")
    assert s["approval_id"] != s["operation_id"]          # approval_id 与 operation_id 独立
    assert s["risk_level"] == "high"
    assert s["evidence_refs"]                              # 只读证据引用非空
    assert isinstance(s["action_draft"], dict)             # 动作草稿解释
    assert s["action_draft"]["amount"] == "100.00"         # 金额来自领域政策计算
    assert s["next_action"] == "wait_approval"
    assert s.get("error_code") is None
