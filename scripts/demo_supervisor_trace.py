"""OpsPilot V1.1 Supervisor 多 Agent 轨迹演示（可复现，真实执行，每步断言）。

运行（项目根，D 盘 .venv）：
    .venv\\Scripts\\python.exe scripts\\demo_supervisor_trace.py [--mode MODE]
    MODE ∈ single_agent | multi_agent | offline_rule | llm（默认 multi_agent）

本脚本展示 FourRoleOrchestrator（Triage → Evidence → Resolution → RiskReview）的
**结构化可复现轨迹**：每角色 trace 记录 trace_id / thread_id / agent_name / role /
input_summary / output_summary / started_at / finished_at / duration_ms / status /
reject_reason / citations / tool_calls，挂在 order_summary.supervisor.traces
（escalate 路径同样随 order_summary 携带）。

模式语义（与 src/agents/modes.py 一致）：
- multi_agent：SupervisorRunner(orchestration="four-role")，真实 start→interrupt→
  approve/reject→resume→execute/unknown 对账闭环（复用单 Agent LangGraph 图，仅证据
  编排替换为四角色）；
- single_agent / offline_rule：同一批场景的 WorkflowRunner 对照（同数据集、同安全约束，
  无 Supervisor 角色轨迹——单 Agent 证据在同一 gather_evidence 节点内完成）；
- llm：按 resolve_mode 回落（未配置安全 Key / 白名单 Base URL → offline_rule），
  输出注明回落；若环境真配置了可用 LLM（resolve 后仍为 llm）→ 本脚本拒绝运行，
  不把离线规则输出冒充真实模型结果。

诚实边界：全部固定种子合成数据；金额/资格/状态/幂等/审批由确定性领域服务裁决；
当前四角色与单 Agent 均为确定性规则实现，**未连接真实 LLM**（模型版本如实标注）。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agents import SupervisorRunner, WorkflowRunner  # noqa: E402
from src.agents.modes import RuntimeMode, resolve_mode  # noqa: E402
from src.agents.multiagent import RiskReviewInput  # noqa: E402
from src.domain.after_sales import (  # noqa: E402
    AfterSalesService,
    Order,
    OrderItem,
    OrderStatus,
    PolicyRule,
    RequestType,
)
from src.domain.after_sales.adapters import MemoryAdapter  # noqa: E402
from src.rag import PolicyDocument, PolicyStore  # noqa: E402

BAR = "=" * 70
_DAMAGED_DOC = dict(
    policy_id="P-DAMAGED-FULL", tenant_id="T1", title="破损退款政策",
    content="签收后 30 天内，商品破损可申请全额退款。客户需提供破损照片作为凭证。",
    version=1,
)


# ---------- 固定种子（合成数据；与 golden_v1 / verify_after_sales 同构） ----------

def _svc(with_policy: bool = True) -> AfterSalesService:
    svc = AfterSalesService()
    svc.seed_order(Order(
        order_id="ORD-1", tenant_id="T1", customer_id="C1",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("100.00"),
        items=[OrderItem(sku="SKU-1", name="演示商品", quantity=1,
                         unit_price=Decimal("100.00"))],
        days_since_sign=2,
    ))
    if with_policy:
        svc.seed_policy(PolicyRule(
            policy_id="P-DAMAGED-FULL", tenant_id="T1", request_type=RequestType.REFUND,
            reason_tags=("damaged",), window_days=30, refund_ratio=Decimal("1.00"),
        ))
    return svc


def _store() -> PolicyStore:
    store = PolicyStore()
    store.register(PolicyDocument(**_DAMAGED_DOC))
    return store


# ---------- 运行器工厂（模式 → runner；真实执行同一 batch 场景） ----------

def make_runner_factory(actual: RuntimeMode):
    """actual ∈ single_agent/offline_rule/multi_agent（llm 未配置已回落，见 main）。"""
    def factory(backend):
        if actual is RuntimeMode.MULTI_AGENT:
            return SupervisorRunner(backend, policy_store=_store(), orchestration="four-role")
        return WorkflowRunner(backend, policy_store=_store())
    return factory


def _traces_of(r) -> list[dict]:
    """从 RunResult.state 读取 supervisor traces（approval/escalate 路径都挂在 order_summary）。"""
    st = r.state or {}
    sup = ((st.get("order_summary") or {}).get("supervisor")) or {}
    return list(sup.get("traces") or [])


def _risk_of(r) -> Optional[dict]:
    st = r.state or {}
    sup = ((st.get("order_summary") or {}).get("supervisor")) or {}
    return sup.get("risk") if sup else None


def _audit_counts(svc: AfterSalesService) -> dict:
    log = svc.audit_log()
    return {"total": len(log),
            "executes": len([e for e in log if e.action == "execute"])}


# ---------- 场景 1：正常退款闭环 ----------

def s1_happy_refund(build: Callable, mode: str, tid: str) -> dict:
    svc = _svc()
    runner = build(MemoryAdapter(svc))
    r = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id=tid)
    assert r.waiting_approval, "应进入人工审批 interrupt"
    draft = r.state["action_draft"]
    traces = _traces_of(r)
    result = {
        "scenario": "正常退款闭环 approve→resume→refunded",
        "thread_id": tid, "request": "订单 ORD-1 商品破损，要求退款",
        "draft_amount": draft["amount"], "traces": traces,
        "risk": _risk_of(r),
        "waiting_approval": True,
    }
    op_id = r.state["operation_id"]
    runner.submit_decision(op_id, "approved")
    final = runner.resume(tid)
    assert final.outcome == "refunded"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    result.update(outcome=final.outcome, refunded=str(svc.refunded_amount("ORD-1")),
                  audit=_audit_counts(svc), side_effect=True)
    return result


# ---------- 场景 2：缺订单信息 → 澄清 interrupt ----------

def s2_missing_order_clarify(build: Callable, mode: str, tid: str) -> dict:
    svc = _svc()
    runner = build(MemoryAdapter(svc))
    r = runner.start("T1", "我要退款", thread_id=tid)
    assert r.waiting_clarify, "缺订单号应进入澄清中断"
    assert svc.audit_log() == [] and svc.refunded_amount("ORD-1") == Decimal("0.00")
    result = {
        "scenario": "缺订单信息 → Triage/parse 澄清 interrupt（不猜测）",
        "thread_id": tid, "request": "我要退款",
        "clarify_questions": (r.interrupt_value or {}).get("questions"),
        "traces": _traces_of(r), "outcome": "clarify",
        "audit": _audit_counts(svc), "side_effect": False,
    }
    # 补参后推进到人工审批（验证澄清闭环真实可继续）
    r2 = runner.resume(tid, payload={"order_id": "ORD-1",
                                     "description": "订单 ORD-1 商品破损，要求退款"})
    assert r2.waiting_approval
    result["after_supplement"] = "waiting_approval"
    return result


# ---------- 场景 3：无政策证据 → 转人工（不虚构） ----------

def s3_no_policy_escalate(build: Callable, mode: str, tid: str) -> dict:
    svc = _svc(with_policy=False)          # 无适用 PolicyRule（RAG 文档也不替代领域资格）
    runner = build(MemoryAdapter(svc))
    r = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id=tid)
    assert r.finished and r.outcome == "escalated"
    assert r.error_code == "AFTER_SALES_POLICY_NOT_FOUND"
    assert not (r.state or {}).get("action_draft")
    result = {
        "scenario": "无政策证据 → 领域 POLICY_NOT_FOUND → 转人工（不虚构政策/金额）",
        "thread_id": tid, "request": "订单 ORD-1 商品破损，要求退款",
        "outcome": r.outcome, "error_code": r.error_code,
        "traces": _traces_of(r), "reply": r.reply,
        "audit": _audit_counts(svc), "side_effect": False, "draft_created": False,
    }
    return result


# ---------- 场景 4：RiskReviewer 阻断风险（构造注入评审；当前规则实现无法自然产出） ----------
#
# 诚实边界：确定性规则实现中，Evidence/工具层已按租户、版本过滤证据，编排自然数据流
# 无法产生跨租户/无效引用；RiskReviewer 是“未来真实 LLM 子 Agent 被污染/伪造”时的防御
# 纵深。此处直接对 RiskReviewer 输入伪造引用做真实评审（review() 真实执行），断言阻断码；
# 编排收到 risk_ok=False 时以 RISK_REVIEW_FAILED 转人工、零副作用（无草稿提交审批）。

def s4_risk_reviewer_blocks(build: Callable, mode: str, tid: str) -> dict:
    from src.agents import FourRoleOrchestrator
    from src.agents.ports import AfterSalesGateway
    svc = _svc()
    orch = FourRoleOrchestrator(AfterSalesGateway(MemoryAdapter(svc)), _store())
    healthy_input = RiskReviewInput(
        tenant_id="T1", order_id="ORD-1", order_status="delivered",
        customer_id="C1", evidence_tenant_id="T1",
        amount_source="domain_refund_plan",
        plan_policy_id="P-DAMAGED-FULL", resolution_policy_id="P-DAMAGED-FULL",
        resolution_action_kind="refund", resolution_draft_intent="refund",
        evidence_refs=["order:ORD-1", "customer:C1", "policy:P-DAMAGED-FULL@1#0"],
        policy_citations=["P-DAMAGED-FULL@1#0"],
    )
    healthy = orch.risk_reviewer.review(healthy_input)
    assert healthy.risk_ok, "健康引用不应被阻断"

    # 构造 1：引用不可校验（模拟 LLM 子 Agent 引用伪造/失效文档）→ RISK_CITATION_INVALID
    forged = healthy_input.model_copy(update={
        "evidence_refs": ["order:ORD-1", "customer:C1", "policy:P-NOPE@1#0"],
        "policy_citations": ["P-NOPE@1#0"],
    })
    bad_cite = orch.risk_reviewer.review(forged)
    assert not bad_cite.risk_ok
    assert any(f.code == "RISK_CITATION_INVALID" for f in bad_cite.findings)
    # 构造 2：跨租户证据（模拟越权/跨租户引用）→ RISK_CROSS_TENANT
    cross = healthy_input.model_copy(update={"evidence_tenant_id": "T9"})
    cross_review = orch.risk_reviewer.review(cross)
    assert not cross_review.risk_ok
    assert any(f.code == "RISK_CROSS_TENANT" for f in cross_review.findings)

    result = {
        "scenario": "RiskReviewer 发现风险并阻止动作（构造：伪造引用/跨租户证据）",
        "thread_id": tid,
        "request": "（评审探针：直接构造 RiskReviewInput，非用户请求驱动）",
        "healthy_risk_ok": healthy.risk_ok,
        "blocking_case_1": {"kind": "RISK_CITATION_INVALID",
                            "findings": [f.code for f in bad_cite.findings]},
        "blocking_case_2": {"kind": "RISK_CROSS_TENANT",
                            "findings": [f.code for f in cross_review.findings]},
        "orchestrator_escalate": "RISK_REVIEW_FAILED（转人工，不继续生成草稿）",
        "side_effect": False,
        "note": ("编排自然数据流不会产生此类风险（工具层已按租户/版本过滤证据）；"
                 "本场景直接评审 RiskReviewer，验证真实 LLM 子 Agent 若被污染的阻断纵深"),
    }
    assert svc.audit_log() == []
    result["audit"] = _audit_counts(svc)
    return result


# ---------- 场景 5：审批拒绝 → 零副作用 ----------

def s5_approval_rejected(build: Callable, mode: str, tid: str) -> dict:
    svc = _svc()
    runner = build(MemoryAdapter(svc))
    r = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id=tid)
    assert r.waiting_approval
    op_id = r.state["operation_id"]
    runner.submit_decision(op_id, "rejected", reason="重复申请")
    final = runner.resume(tid)
    assert final.outcome == "rejected"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    counts = _audit_counts(svc)
    assert counts["executes"] == 0
    return {
        "scenario": "审批拒绝 → resume 零副作用（不执行、不退款）",
        "thread_id": tid, "request": "订单 ORD-1 商品破损，要求退款",
        "traces": _traces_of(r), "outcome": final.outcome,
        "refunded": str(svc.refunded_amount("ORD-1")), "audit": counts,
        "side_effect": False,
    }


# ---------- 场景 6：外部执行 unknown → 仅原 operation_id 对账 ----------

def s6_unknown_reconcile_original_key(build: Callable, mode: str, tid: str) -> dict:
    svc = _svc()
    runner = build(MemoryAdapter(svc))
    r = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id=tid,
                     simulate_external="timeout")
    assert r.waiting_approval
    op_id = r.state["operation_id"]
    runner.submit_decision(op_id, "approved")
    r2 = runner.resume(tid)
    assert r2.outcome == "operation_unknown"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    runner.reconcile_unknown(op_id, "success")
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    return {
        "scenario": "外部执行 unknown → 仅原 operation_id 对账 success → 落账",
        "thread_id": tid, "request": "订单 ORD-1 商品破损，要求退款（外部 timeout）",
        "traces": _traces_of(r), "outcome": r2.outcome,
        "reconciled": "success（原键，未换键重试）",
        "refunded": str(svc.refunded_amount("ORD-1")), "audit": _audit_counts(svc),
        "side_effect": True,
    }


# ---------- 场景 7：跨租户查询被拒绝 ----------

def s7_cross_tenant_rejected(build: Callable, mode: str, tid: str) -> dict:
    svc = _svc()
    runner = build(MemoryAdapter(svc))
    r = runner.start("T2", "订单 ORD-1 商品破损，要求退款", thread_id=tid)
    assert r.finished and r.outcome == "escalated"
    assert r.error_code == "AFTER_SALES_TENANT_MISMATCH"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    counts = _audit_counts(svc)
    assert counts["executes"] == 0
    return {
        "scenario": "跨租户查询被拒绝（T2 读 T1 订单 → escalated 零副作用）",
        "thread_id": tid, "request": "订单 ORD-1 商品破损，要求退款（tenant=T2）",
        "traces": _traces_of(r), "outcome": r.outcome, "error_code": r.error_code,
        "reply": r.reply, "audit": counts, "side_effect": False,
    }


# ---------- 场景 8：同一幂等键重复请求 → 原结果，execute 审计仅 1 条 ----------

def s8_repeat_request_idempotent(build: Callable, mode: str, tid: str) -> dict:
    svc = _svc()
    runner = build(MemoryAdapter(svc))
    r1 = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id=tid)
    assert r1.waiting_approval
    first_traces = _traces_of(r1)
    op_id = r1.state["operation_id"]
    runner.submit_decision(op_id, "approved")
    f1 = runner.resume(tid)
    assert f1.outcome == "refunded"
    r2 = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id=tid)
    counts = _audit_counts(svc)
    assert counts["executes"] == 1, "重复请求不得新增 execute 审计"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    return {
        "scenario": "同一幂等键重复请求 → 返回原结果（execute 审计仅 1 条）",
        "thread_id": tid, "request": "订单 ORD-1 商品破损，要求退款（重复提交）",
        "traces": first_traces, "first_outcome": f1.outcome,
        "repeat_outcome": r2.outcome, "refunded": str(svc.refunded_amount("ORD-1")),
        "audit": counts, "side_effect": True, "repeat_side_effect": False,
    }


_SCENARIOS: list[Callable] = [
    s1_happy_refund, s2_missing_order_clarify, s3_no_policy_escalate,
    s4_risk_reviewer_blocks, s5_approval_rejected, s6_unknown_reconcile_original_key,
    s7_cross_tenant_rejected, s8_repeat_request_idempotent,
]


# ---------- 打印 ----------

def _print_trace_block(thread: str, traces: list[dict]) -> None:
    if not traces:
        return
    print(f"    —— 四角色执行轨迹（真实执行顺序，trace_id 全部 = ma-{thread}:*）——")
    for idx, t in enumerate(traces, 1):
        print(f"    {idx}) {t['agent_name']}  status={t['status']}  "
              f"duration_ms={t['duration_ms']}  reject_reason={t['reject_reason'] or '-'}")
        print(f"       trace_id     : {t['trace_id']}")
        print(f"       input_summary: {t['input_summary']}")
        print(f"       output_summary: {t['output_summary']}")
        print(f"       tool_calls   : {t['tool_calls']}")
        print(f"       citations    : {t['citations']}")


def _print_result(res: dict, mode: str, multi: bool, total_elapsed: float) -> None:
    print(f"  场景        : {res['scenario']}")
    print(f"  请求摘要    : {res['request']}")
    print(f"  thread_id   : {res['thread_id']}")
    if multi and res.get("traces"):
        _print_trace_block(res["thread_id"], res["traces"])
    elif multi and not res.get("traces"):
        print("    （本场景未进入四角色编排——澄清 interrupt 在 parse 阶段拦截，先于 gather_evidence；"
              "无角色轨迹）")
    elif not multi:
        print("    （单 Agent 对照：无 Supervisor 角色轨迹，证据在单个 gather_evidence 节点内完成）")
    if res.get("risk") is not None:
        rk = res["risk"]
        print(f"  RiskReviewer : risk_ok={rk.get('risk_ok')} risk_level={rk.get('risk_level')} "
              f"findings={[f['code'] for f in rk.get('findings', [])]}")
    if res.get("blocking_case_1") or res.get("blocking_case_2"):
        print(f"  RiskReviewer(直接评审): healthy_risk_ok={res.get('healthy_risk_ok')}")
        for k in ("blocking_case_1", "blocking_case_2"):
            bc = res.get(k)
            if bc:
                print(f"    阻断 {bc['kind']}：findings={bc['findings']} → 编排 {res.get('orchestrator_escalate')}")
        print(f"    {res.get('note', '')}")
    if res.get("draft_amount"):
        print(f"  动作草稿    : 已创建（金额 {res['draft_amount']} 元，确定性领域计划；无金额来自 Agent）")
    if res.get("draft_created") is False:
        print("  动作草稿    : 未创建（不虚构政策/金额）")
    if res.get("waiting_approval"):
        print("  人工审批    : 进入（interrupt）→ 授权人员提交决定 → resume 重读领域决定")
    if res.get("clarify_questions") is not None:
        print(f"  澄清 interrupt: {res['clarify_questions']}")
        print(f"  补参后        : {res.get('after_supplement')}")
    if res.get("outcome"):
        print(f"  最终结果    : outcome={res['outcome']}"
              + (f"（error_code={res['error_code']}）" if res.get("error_code") else ""))
    if res.get("reply"):
        print(f"  reply       : {res['reply']}")
    if res.get("first_outcome"):
        print(f"  首次/重复    : first={res['first_outcome']} → repeat={res['repeat_outcome']} "
              f"（repeat_side_effect={res.get('repeat_side_effect')}）")
    if res.get("refunded") is not None:
        print(f"  副作用-退款  : {res['refunded']} 元")
    if res.get("reconciled"):
        print(f"  对账         : {res['reconciled']}（unknown 阶段金额 0，未盲目入账）")
    if res.get("audit") is not None:
        print(f"  副作用-审计  : {res['audit']['total']} 条（其中 execute {res['audit']['executes']} 次）")
    print(f"  场景耗时    : 累计 {total_elapsed:.3f} s（stdout 计时，非报告数据）")


def run_demo(mode: str = "multi_agent") -> list[dict]:
    """跑全部 8 个场景（真实执行 + 断言），返回结构化结果（可被测试复用）。

    mode：请求的运行模式字符串；resolve 后 actual∈{single_agent, offline_rule, multi_agent}
    （llm 未配置 Key 回落 offline_rule）。每个场景 fresh 领域服务/运行器，固定种子。
    """
    requested = mode
    actual = resolve_mode(requested)
    if actual is RuntimeMode.LLM:
        raise RuntimeError(
            "环境已配置可用 LLM（OPSPILOT_LLM_API_KEY + 白名单 Base URL）：本演示只跑确定性"
            "规则路径，拒绝把离线输出冒充真实模型结果；请显式使用 single_agent/multi_agent。")
    multi = actual is RuntimeMode.MULTI_AGENT
    build = make_runner_factory(actual)
    print(f"\n{BAR}\nOpsPilot V1.1 Supervisor 多 Agent 轨迹演示（确定性规则，真实执行，固定种子合成数据）\n{BAR}")
    print(f"请求模式    : {requested}  ->  resolve 后实际模式：{actual.value}"
          + ("（llm 未配置安全 Key / 白名单 Base URL，按 modes.resolve_mode 回落离线规则，不静默直连）"
             if actual is RuntimeMode.OFFLINE_RULE and requested == "llm" else ""))
    print(f"运行器      : " + ("SupervisorRunner(orchestration='four-role')，四角色编排挂接单 Agent 图"
                              if multi else "WorkflowRunner（单 Agent 证据编排，与 Supervisor 同安全约束）"))
    print("模型版本    : offline / rules-v1（四角色与单 Agent 均为确定性规则实现）")
    print("Prompt 版本 : N/A（无 LLM 无 Prompt）")
    print("数据集版本  : golden-v1 合成（固定种子，不代表真实企业数据）")
    print("online_llm  : not_configured（未连接真实 LLM；结果不得视为真实模型输出）")
    results: list[dict] = []
    t_demo = time.monotonic()
    for idx, fn in enumerate(_SCENARIOS, 1):
        tid = f"tr-s{idx}"
        print(f"\n{BAR}\n[场景 {idx}] {fn.__name__}（mode={actual.value}）\n{BAR}")
        t0 = time.monotonic()
        res = fn(build, actual.value, tid)
        res["scenario_no"] = str(idx)
        res["elapsed_s"] = round(time.monotonic() - t0, 3)
        _print_result(res, actual.value, multi, res["elapsed_s"])
        results.append(res)
    demo_elapsed = time.monotonic() - t_demo
    print(f"\n{BAR}\n8/8 场景通过（真实执行 + 断言）：审批/resume/unknown 对账/幂等均经确定性领域服务；"
          f"四角色轨迹含每角色耗时/输入输出摘要/工具调用/引用。总演示耗时 {demo_elapsed:.3f} s\n{BAR}")
    return results


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="OpsPilot V1.1 Supervisor 多 Agent 轨迹演示（确定性规则，可复现）")
    parser.add_argument("--mode", default="multi_agent",
                        choices=[m.value for m in RuntimeMode],
                        help="请求运行模式；默认 multi_agent。llm 未配置 Key 按 resolve_mode 回落 offline_rule。")
    args = parser.parse_args(argv)
    run_demo(args.mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
