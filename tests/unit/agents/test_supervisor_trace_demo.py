"""V1.1 Supervisor 四角色轨迹（order_summary.supervisor.traces）与演示/对照脚本可复现测试。

覆盖（不破坏既有 14+11+7 语义，仅新增断言）：
1) four-role 编排 happy path：traces 含 4 角色记录、字段齐全（trace_id/thread_id/
   agent_name/role/input_summary/output_summary/started_at/finished_at/duration_ms/
   status/citations/tool_calls）、无 PII（电话/邮箱已掩码）；
2) escalate 路径：traces 随 order_summary 返回，resolution error / risk skipped 的
   reject_reason 与域错误码对应；跨租户在 evidence 阶段 error 且零副作用；
3) RiskReviewer 阻断：trace status=escalated、reject_reason=RISK_* 码、编排转人工；
4) 演示可复现：scripts/demo_supervisor_trace.run_demo 在 multi_agent / single_agent
   下均跑通 8 场景（真实执行 + 内部断言），返回结构化结果；
5) 对照冒烟：evals/compare_modes._run 子集跑通且确定性规则下三种模式通过率一致。
"""
import json
import re
from decimal import Decimal

import pytest

from src.agents import SupervisorRunner, WorkflowRunner
from src.agents.modes import RuntimeMode
from src.agents.multiagent import (
    EVIDENCE_AGENT_SPEC,
    FOUR_ROLE_SPECS,
    RESOLUTION_AGENT_SPEC,
    RISK_REVIEWER_SPEC,
    TRIAGE_AGENT_SPEC,
    FourRoleOrchestrator,
    RiskFinding,
    RiskReviewOutput,
)
from src.agents.ports import AfterSalesGateway
from src.agents.state import new_state
from src.domain.after_sales import AfterSalesService
from src.domain.after_sales.adapters import MemoryAdapter
from src.rag import PolicyDocument, PolicyStore
from tests.unit.domain.after_sales.helpers import make_order

import scripts.demo_supervisor_trace as demo  # noqa: E402
from evals import compare_modes  # noqa: E402

REQUEST = "订单 ORD-1 商品破损，要求退款"
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_DAMAGED_DOC = dict(
    policy_id="P-DAMAGED-FULL", tenant_id="T1", title="破损退款政策",
    content="签收后 30 天内，商品破损可申请全额退款。客户需提供破损照片作为凭证。",
    version=1,
)

_TRACE_KEYS = {
    "trace_id", "thread_id", "agent_name", "role", "input_summary",
    "output_summary", "started_at", "finished_at", "duration_ms",
    "status", "reject_reason", "citations", "tool_calls",
}


def make_svc_store(with_policy: bool = True):
    svc = AfterSalesService()
    svc.seed_order(make_order())
    if with_policy:
        from src.domain.after_sales import PolicyRule, RequestType
        svc.seed_policy(PolicyRule(
            policy_id="P-DAMAGED-FULL", tenant_id="T1", request_type=RequestType.REFUND,
            reason_tags=("damaged",), window_days=30, refund_ratio=Decimal("1.00"),
        ))
    store = PolicyStore()
    store.register(PolicyDocument(**_DAMAGED_DOC))
    return svc, store


def state_for(thread: str = "t1") -> dict:
    st = dict(new_state())
    st.update(tenant_id="T1", thread_id=thread, user_request=REQUEST,
              order_id="ORD-1", intent="refund", reason_tags=["damaged"])
    return st


def _orchestrator(with_policy: bool = True) -> FourRoleOrchestrator:
    svc, store = make_svc_store(with_policy=with_policy)
    return FourRoleOrchestrator(AfterSalesGateway(MemoryAdapter(svc)), store)


def _flat(traces: list[dict]) -> str:
    return json.dumps(traces, ensure_ascii=False)


# ---------- 1) happy path：traces 结构 / 字段 / 顺序 / 无 PII ----------

def test_four_role_happy_traces_structure_and_fields():
    orch = _orchestrator()
    out = orch.run(state_for())
    assert out.get("error_code") is None
    sup = out["order_summary"]["supervisor"]
    # 追加 traces 不破坏既有 supervisor key
    assert set(sup) >= {"orchestration", "agents", "triage", "resolution", "risk", "traces"}
    traces = sup["traces"]
    assert len(traces) == 4
    assert [t["agent_name"] for t in traces] == [s.name for s in FOUR_ROLE_SPECS]
    for t, spec in zip(traces, FOUR_ROLE_SPECS):
        assert set(t) == _TRACE_KEYS
        assert t["trace_id"] == f"ma-t1:{spec.name}"
        assert t["thread_id"] == "t1"
        assert t["agent_name"] == spec.name
        assert t["role"] == spec.role
        assert isinstance(t["input_summary"], str) and t["input_summary"]
        assert isinstance(t["output_summary"], str) and t["output_summary"]
        assert re.fullmatch(r"\d+\.\d{3}", t["started_at"]) is not None
        assert re.fullmatch(r"\d+\.\d{3}", t["finished_at"]) is not None
        assert isinstance(t["duration_ms"], float) and t["duration_ms"] >= 0
        assert t["status"] == "ok" and t["reject_reason"] == ""
        assert isinstance(t["citations"], list) and isinstance(t["tool_calls"], list)
    # evidence/risk 记录引用；evidence 记录真实工具调用（白名单 3 只读工具）
    assert traces[1]["citations"] == ["P-DAMAGED-FULL@1#0"]
    assert traces[1]["tool_calls"] == ["get_order", "list_customer_tickets", "retrieve_policy"]
    assert traces[3]["citations"] == ["P-DAMAGED-FULL@1#0"]
    assert traces[0]["tool_calls"] == [] and traces[2]["tool_calls"] == []
    # 无完整 PII（电话/邮箱）
    blob = _flat(traces)
    assert _PHONE_RE.search(blob) is None and _EMAIL_RE.search(blob) is None


def test_four_role_traces_mask_phone_and_email_in_user_request():
    """用户请求自带联系方式 → trace input_summary 掩码，不落完整 PII。"""
    orch = _orchestrator()
    st = state_for()
    st["user_request"] = ("订单 ORD-1 商品破损要求退款，联系 13800138000，"
                          "邮箱 owner@example.com")
    out = orch.run(st)
    assert out.get("error_code") is None
    triage_in = out["order_summary"]["supervisor"]["traces"][0]["input_summary"]
    assert "13800138000" not in triage_in and "owner@example.com" not in triage_in
    assert "<phone>" in triage_in and "<email>" in triage_in
    assert _PHONE_RE.search(_flat(out["order_summary"]["supervisor"]["traces"])) is None
    assert _EMAIL_RE.search(_flat(out["order_summary"]["supervisor"]["traces"])) is None


# ---------- 2) escalate 路径：traces 随 order_summary 返回 + reject_reason 对应 ----------

def test_four_role_policy_missing_escalate_traces_reject_reason():
    orch = _orchestrator(with_policy=False)   # 领域无适用政策
    out = orch.run(state_for())
    assert out["error_code"] == "AFTER_SALES_POLICY_NOT_FOUND"
    assert out["outcome"] == "escalated"
    traces = out["order_summary"]["supervisor"]["traces"]
    assert [t["status"] for t in traces] == ["ok", "ok", "error", "skipped"]
    assert traces[0]["agent_name"] == "triage-agent"
    assert traces[2]["reject_reason"] == "AFTER_SALES_POLICY_NOT_FOUND"  # 域错误码原样透传
    assert traces[3]["reject_reason"] == "AFTER_SALES_POLICY_NOT_FOUND"
    # 无引用被带入写路径：无草稿、零副作用（仅编排只读）
    assert out.get("action_draft") is None


def test_four_role_cross_tenant_evidence_error_trace_zero_side_effect():
    svc, store = make_svc_store()
    sup = SupervisorRunner(MemoryAdapter(svc), policy_store=store,
                           orchestration="four-role")
    r = sup.start("T2", REQUEST, thread_id="tr-x")
    assert r.finished and r.outcome == "escalated"
    assert r.error_code == "AFTER_SALES_TENANT_MISMATCH"
    traces = r.state["order_summary"]["supervisor"]["traces"]
    assert len(traces) == 4
    assert traces[1]["status"] == "error"
    assert traces[1]["reject_reason"] == "AFTER_SALES_TENANT_MISMATCH"
    assert traces[2]["status"] == traces[3]["status"] == "skipped"
    assert svc.audit_log() == [] and svc.refunded_amount("ORD-1") == Decimal("0.00")


# ---------- 3) RiskReviewer 阻断 → trace status=escalated + reject_reason ----------

def test_risk_blocking_trace_status_escalated_and_reject_reason(monkeypatch):
    orch = _orchestrator()

    def fake_review(input_):  # noqa: ANN001
        return RiskReviewOutput(
            risk_ok=False, risk_level="high",
            findings=[RiskFinding(code="RISK_CITATION_INVALID",
                                  detail="政策引用不可校验：P-NOPE@1#0")])
    monkeypatch.setattr(orch.risk_reviewer, "review", fake_review)
    out = orch.run(state_for())
    assert out["error_code"] == "RISK_REVIEW_FAILED"
    assert out["outcome"] == "escalated"
    traces = out["order_summary"]["supervisor"]["traces"]
    risk_trace = traces[3]
    assert risk_trace["agent_name"] == "risk-reviewer"
    assert risk_trace["status"] == "escalated"
    assert risk_trace["reject_reason"] == "RISK_CITATION_INVALID"
    assert out["order_summary"]["supervisor"]["risk"]["risk_ok"] is False


# ---------- 4) Supervisor four-role 端到端：approval interrupt 带 4 条 ok traces ----------

def test_four_role_supervisor_approval_state_carries_traces():
    svc, store = make_svc_store()
    sup = SupervisorRunner(MemoryAdapter(svc), policy_store=store,
                           orchestration="four-role")
    r = sup.start("T1", REQUEST, thread_id="tr-apr")
    assert r.waiting_approval
    sup_sub = r.state["order_summary"]["supervisor"]
    traces = sup_sub["traces"]
    assert len(traces) == 4
    assert all(t["status"] == "ok" for t in traces)
    assert sup_sub["risk"]["risk_ok"] is True
    # 轨迹 thread_id 与流程线程一致（trace_id=ma-{thread}:{agent}）
    assert all(t["thread_id"] == "tr-apr" for t in traces)
    assert all(t["trace_id"].startswith("ma-tr-apr:") for t in traces)


# ---------- 5) demo 可复现（函数级调用，真实执行 + 内部断言） ----------

def test_demo_multi_agent_run_reproducible(capsys):
    results = demo.run_demo("multi_agent")
    capsys.readouterr()                       # 吞掉演示 stdout
    assert len(results) == 8
    by_no = {res["scenario_no"]: res for res in results}
    assert by_no["1"]["outcome"] == "refunded"
    assert by_no["1"]["draft_amount"] == "100.00"
    assert by_no["2"]["outcome"] == "clarify"
    assert by_no["3"]["error_code"] == "AFTER_SALES_POLICY_NOT_FOUND"
    assert by_no["5"]["outcome"] == "rejected"
    assert by_no["6"]["outcome"] == "operation_unknown"
    assert by_no["6"]["refunded"] == "100.00"           # 原键对账成功
    assert by_no["7"]["error_code"] == "AFTER_SALES_TENANT_MISMATCH"
    assert by_no["8"]["audit"]["executes"] == 1         # 重复请求不重复副作用
    # 场景 1 的 supervisor 轨迹在演示结构化结果中可复现（4 角色、ok）
    traces = by_no["1"]["traces"]
    assert len(traces) == 4 and all(t["status"] == "ok" for t in traces)
    assert all(t["trace_id"].startswith("ma-tr-s1:") for t in traces)


def test_demo_single_agent_run_reproducible(capsys):
    results = demo.run_demo("single_agent")
    capsys.readouterr()
    assert len(results) == 8
    assert results[0]["outcome"] == "refunded"
    # 单 Agent 对照不产生 Supervisor 角色轨迹
    assert results[0]["traces"] == []
    assert results[2]["outcome"] == "escalated"
    assert results[2]["error_code"] == "AFTER_SALES_POLICY_NOT_FOUND"


def test_demo_mode_multi_agent_uses_four_role_runtime():
    """multi_agent resolve 后为 RuntimeMode.MULTI_AGENT（four-role 运行时语义由上面端到端验证）。"""
    assert demo.resolve_mode("multi_agent") is RuntimeMode.MULTI_AGENT


# ---------- 6) compare_modes 冒烟：子集跑通 + 确定性规则下三种模式通过率一致 ----------

def test_compare_modes_smoke_subset_rates_equal_and_report_renderable():
    result = compare_modes._run(limit=3)
    assert result["total"] == 3
    assert result["pass"]["single"] == result["pass"]["three"] == result["pass"]["four"] == 3
    assert result["outcome_consistent"] == 3
    assert result["refund_consistent"] == 3
    # 报告可生成（确定性渲染；不含逐 run 耗时）
    rendered = compare_modes._render(result)
    assert "# 运行模式 A/B 对照报告" in rendered
    assert "only stdout" not in rendered          # 耗时不以数字入报告（保确定性）
    assert "not_configured" in rendered           # online_llm 如实标注
