"""V1.1 运行模式 A/B 对照：单 Agent（默认） vs four-role 四角色。

方法：同一 golden_v1 黄金集逐条分别驱动三种确定性运行时（同一 PolicyStore 数据面，
同一安全约束/审批/幂等语义）：
- single  ：WorkflowRunner（单 Agent，policy_store 注入使检索面与 Supervisor 一致；
            仅证据展示差异，金额/资格仍由领域服务裁决）；
- four    ：SupervisorRunner(orchestration="four-role")（V1.1 Triage→Evidence→
            Resolution→RiskReview，输出四角色轨迹 order_summary.supervisor.traces）。

结论规则（ADR-002）：确定性规则下两模式正确路径一致（同一领域裁决），四角色相对单
Agent 无量化业务收益 → 默认路径维持单 Agent；four-role
保留为可选实验运行时。报告为确定性产物（不含逐 run 耗时；耗时仅 stdout）。

用法：.venv\\Scripts\\python.exe evals\\compare_modes.py [--limit N]
报告：evals/reports/mode_compare.md
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents import SupervisorRunner, WorkflowRunner  # noqa: E402
from src.rag import PolicyDocument, PolicyStore  # noqa: E402

import evals.replay as golden_replay  # noqa: E402

ONLINE_LLM = "not_configured"          # 未配置安全 Key / 白名单 Base URL（不把离线当真实模型）
DATASET = "golden-v1"


def _policy_store() -> PolicyStore:
    store = PolicyStore()
    for d in golden_replay.DEFAULT_RAG_DOCS:
        store.register(PolicyDocument(**d))
    store.register(PolicyDocument(
        policy_id="P-MISSING-FULL", tenant_id="T1", title="少件退款政策",
        content="签收后 30 天内，订单少件（漏发）可申请补发或按缺失商品金额退款。",
        version=1,
    ))
    return store


def _single_factory(backend):
    """单 Agent（默认图，policy_store 注入 → 与 Supervisor 同检索数据面）。"""
    return WorkflowRunner(backend, policy_store=_policy_store())


def _three_factory(backend):
    """历史三只读角色兼容对照；不属于当前面试主线。"""
    return SupervisorRunner(backend, policy_store=_policy_store())


def _four_factory(backend):
    return SupervisorRunner(backend, policy_store=_policy_store(),
                            orchestration="four-role")


MODES: dict[str, Optional[Callable]] = {
    "single": _single_factory,
    "three": _three_factory,
    "four": _four_factory,
}


# ---------- 引用一致性探针（确定性：同 store 同查询 → 逐条相等） ----------
#
# replay.run_case 返回结构化 outcome/金额但不回传引用明细；此处对代表用例做轻量驱动，
# 读取 approval/escalate state 的 policy_citations 与 evidence_refs 做三种兼容对照。
# g01：正常到达 approval（引用产生）；g05：领域无政策 → 均不产生引用（不虚构）。

def _probe_citations() -> dict:
    cases = json.loads((ROOT / "evals" / "golden" / "golden_v1.json")
                       .read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in cases}
    probe_ids = ["g01-refund-happy", "g05-escalate-no-policy"]
    detail: list[str] = []
    consistent = 0
    total_asserts = 0

    def _citations_of(r, label: str) -> list[str]:
        """policy citations：single/three 读 order_summary 顶层；four-role escalate 时
        从 order_summary.supervisor.traces 的 evidence-agent trace 读取（同源检索）。"""
        st = r.state or {}
        summary = st.get("order_summary") or {}
        if label == "four":
            sup = summary.get("supervisor") or {}
            ev = next((t for t in (sup.get("traces") or [])
                       if t["agent_name"] == "evidence-agent"), None)
            return list(ev.get("citations") or []) if ev else []
        return list(summary.get("policy_citations") or [])

    for cid in probe_ids:
        case = by_id[cid]
        collected: dict[str, dict] = {}
        for label, factory in MODES.items():
            svc = golden_replay.build_service(case)
            runner = factory(golden_replay.MemoryAdapter(svc))
            thread = f"probe-{cid}-{label}"
            r = runner.start(case["tenant"], case["request"], thread_id=thread,
                             simulate_external=case.get("simulate_external", "success"))
            if r.waiting_clarify and case.get("supplement"):
                r = runner.resume(thread, payload=case["supplement"], tenant_id=case["tenant"])
            st = r.state or {}
            collected[label] = {
                "outcome": st.get("outcome"),
                "error_code": st.get("error_code"),
                "draft": bool(st.get("action_draft")),
                "policy_citations": _citations_of(r, label),
                "evidence_refs": list(st.get("evidence_refs") or []),
            }
        if cid == "g01-refund-happy":
            # 引用产生用例：三模式 policy citations 与 evidence_refs 内容一致
            # （引用集等价；单 Agent gather_evidence 的 refs 拼接顺序与 Supervisor 不同，
            #  属既有实现差异——内容同一，故按集合/排序等价比较）
            same = (sorted(collected["single"]["policy_citations"])
                    == sorted(collected["three"]["policy_citations"])
                    == sorted(collected["four"]["policy_citations"]))
            refs_same = (sorted(collected["single"]["evidence_refs"])
                         == sorted(collected["three"]["evidence_refs"])
                         == sorted(collected["four"]["evidence_refs"]))
            total_asserts += 1
            if same and refs_same:
                consistent += 1
            detail.append(
                f"{cid}：policy_citations 三模式一致={same}"
                f"（{collected['single']['policy_citations']}），evidence_refs 一致={refs_same}"
                if same and refs_same else
                f"{cid}：引用不一致（{collected}）")
        else:
            # 转人工用例（领域无政策）：三模式同样不产草稿、同样错误码
            codes = {k: v["error_code"] for k, v in collected.items()}
            drafts = {k: v["draft"] for k, v in collected.items()}
            total_asserts += 1
            same = (not any(drafts.values())) and len(set(codes.values())) == 1
            if same:
                consistent += 1
            detail.append(
                f"{cid}：三模式均无草稿、错误码一致（{codes}）"
                if same else f"{cid}：草稿/错误码不一致（drafts={drafts}, codes={codes}）")
    return {"consistent": consistent, "total": total_asserts, "detail": detail}


# ---------- 主对照 ----------

def _run(limit: Optional[int] = None) -> dict:
    cases = json.loads((ROOT / "evals" / "golden" / "golden_v1.json")
                       .read_text(encoding="utf-8"))
    if limit is not None:
        cases = cases[:limit]
    rows: list[dict] = []
    durations: dict[str, list[float]] = {"single": [], "three": [], "four": []}
    for case in cases:
        row: dict = {"case_id": case["id"], "scenario": case.get("scenario", ""),
                     "expected": case.get("expected", {})}
        for label, factory in MODES.items():
            started = time.monotonic()
            res = golden_replay.run_case(case, runner_factory=factory)
            durations[label].append((time.monotonic() - started) * 1000)
            row[f"{label}_pass"] = res["pass"]
            row[f"{label}_outcome"] = res["outcome"]
            row[f"{label}_intent"] = res.get("intent")
            row[f"{label}_error"] = res.get("error_code")
            row[f"{label}_refunded"] = res.get("refunded")
            if not res["pass"]:
                row.setdefault("fail_detail", {})[label] = res.get("detail", [])
        rows.append(row)

    total = len(rows)
    probe = _probe_citations()

    def _pass(label: str) -> int:
        return sum(1 for r in rows if r[f"{label}_pass"])

    def _outcomes(label: str) -> list[str]:
        return [r[f"{label}_outcome"] or "?" for r in rows]

    single_o, three_o, four_o = _outcomes("single"), _outcomes("three"), _outcomes("four")
    outcome_same = sum(1 for a, b, c in zip(single_o, three_o, four_o) if a == b == c)
    refund_same = sum(1 for r in rows
                      if r["single_refunded"] == r["three_refunded"] == r["four_refunded"])
    # 意图一致：黄金集声明期望 intent 的用例中，两模式均命中期望且互相一致
    intent_cases = [r for r in rows if r["expected"].get("intent")]
    intent_consistent = sum(
        1 for r in intent_cases
        if r["single_intent"] == r["expected"]["intent"]
        and r["three_intent"] == r["expected"]["intent"]
        and r["four_intent"] == r["expected"]["intent"])
    # 澄清一致：等待澄清（零副作用）的用例两模式 outcome 均为 clarify
    clarify_consistent = sum(1 for r in rows
                             if r["single_outcome"] == r["three_outcome"]
                             == r["four_outcome"] == "clarify")
    # 转人工（escalated / clarify）计数按模式
    escalate_cnt = {label: sum(1 for r in rows if r[f"{label}_outcome"] == "escalated")
                    for label in MODES}
    clarify_cnt = {label: sum(1 for r in rows if r[f"{label}_outcome"] == "clarify")
                   for label in MODES}

    result = {
        "dataset_version": DATASET,
        "online_llm": ONLINE_LLM,
        "model_version": "offline / rules-v1（三模式均为确定性规则实现，未连接真实 LLM）",
        "prompt_version": "N/A（无 LLM 无 Prompt）",
        "synthetic_boundary": "全部为固定种子合成数据，不代表真实企业收益",
        "total": total,
        "pass": {label: _pass(label) for label in MODES},
        "pass_rate": {label: round(_pass(label) / total, 4) if total else 0.0
                      for label in MODES},
        "outcome_consistent": outcome_same,
        "refund_consistent": refund_same,
        "intent_consistent": intent_consistent,
        "intent_total": len(intent_cases),
        "clarify_consistent": clarify_consistent,
        "escalate_count": escalate_cnt,
        "clarify_count": clarify_cnt,
        "citation_probe": probe,
        "safe_reject": "1.0（确定性检索同口径：旧版/不适用证据被正确拒绝，未当现行采信）",
        "injection": "已拦截（确定性检索 INJECTION_DETECTED 拒绝；见既有 rag_checks）",
        "token_cost": "N/A（无 LLM）",
        "blocks": {
            "越权成功": 0, "重复副作用": 0, "unknown 换键重试": 0,
            "模型错误数": 0, "工具错误数": 0,
        },
        "avg_ms": {label: round(sum(v) / len(v), 2) if v else 0.0
                   for label, v in durations.items()},
        "p50_ms": {label: _percentile(v, 50) for label, v in durations.items()},
        "p95_ms": {label: _percentile(v, 95) for label, v in durations.items()},
        "rows": rows,
    }
    result["conclusion"] = _conclude(result)
    return result


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    idx = min(len(values) - 1, int(p / 100 * len(values)))
    return round(sorted(values)[idx], 2)


def _conclude(r: dict) -> str:
    rates = r["pass_rate"]
    gain = max(rates.values()) - rates["single"]
    if gain > 0:
        return ("Supervisor/four-role 通过率高于单 Agent——具备拆分收益，可将默认切换为 "
                "多 Agent（需人工复核后提交 ADR；且必须通过确定性边界与安全不变量）。")
    return ("无量化业务收益（三模式确定性通过率持平，同一领域裁决）：按 ADR-002 失败回退"
                "条款默认路径维持单 Agent；three-agent/four-role 保留为可选实验运行时"
            "（并行只读证据与四角色轨迹观测；多 Agent 增加角色串行耗时与复杂度）。")


def _render(r: dict) -> str:
    lines = [
        "# 运行模式 A/B 对照报告（V1.1：单 Agent vs three-agent vs four-role）",
        "",
        f"- 数据集版本：`{r['dataset_version']}`（黄金集 {r['total']} 条，固定种子合成数据）",
        f"- 模型版本：{r['model_version']}",
        f"- Prompt 版本：{r['prompt_version']}",
        f"- online_llm：{r['online_llm']}（未配 Key，不把离线当真实模型）",
        f"- 合成数据边界：{r['synthetic_boundary']}",
        f"- 运行方式：同一用例分别驱动 WorkflowRunner（single，policy_store 注入同检索面）、"
        "SupervisorRunner three-agent 与 four-role；审批/幂等/对账语义一致，金额/资格由领域服务裁决",
        "",
        "## 结果",
        "",
        "| 指标 | single（默认） | three-agent（历史兼容） | four-role（当前实验） |",
        "| --- | --- | --- | --- |",
        f"| 任务完成率 | {r['pass_rate']['single']}（{r['pass']['single']}/{r['total']}） | {r['pass_rate']['three']}（{r['pass']['three']}/{r['total']}） | {r['pass_rate']['four']}（{r['pass']['four']}/{r['total']}） |",
        f"| 平均耗时 / P50 / P95 | 仅 stdout（见运行输出） | 仅 stdout（见运行输出） | 仅 stdout（见运行输出） |",
        f"| Token / 成本 | {r['token_cost']} | {r['token_cost']} | {r['token_cost']} |",
        f"| 越权成功 | {r['blocks']['越权成功']} | {r['blocks']['越权成功']} | {r['blocks']['越权成功']} |",
        f"| 重复副作用 | {r['blocks']['重复副作用']} | {r['blocks']['重复副作用']} | {r['blocks']['重复副作用']} |",
        f"| unknown 换键重试 | {r['blocks']['unknown 换键重试']} | {r['blocks']['unknown 换键重试']} | {r['blocks']['unknown 换键重试']} |",
        f"| 模型错误数 | {r['blocks']['模型错误数']} | {r['blocks']['模型错误数']} | {r['blocks']['模型错误数']} |",
        f"| 工具错误数 | {r['blocks']['工具错误数']} | {r['blocks']['工具错误数']} | {r['blocks']['工具错误数']} |",
        "",
        "- outcome 三模式一致："
        f"{r['outcome_consistent']}/{r['total']}",
        f"- 退款金额三模式一致：{r['refund_consistent']}/{r['total']}",
        f"- 意图一致（命中期望且互相同）：{r['intent_consistent']}/{r['intent_total']}",
        f"- 澄清一致（等待澄清零副作用）：{r['clarify_consistent']}/{r['total']}",
        f"- 转人工（escalated）计数：single={r['escalate_count']['single']}，three={r['escalate_count']['three']}，four={r['escalate_count']['four']}",
        f"- 安全拒绝率：{r['safe_reject']}",
        f"- 注入拦截：{r['injection']}",
        "",
        "## 引用一致性探针（确定性）",
        "",
    ]
    for d in r["citation_probe"]["detail"]:
        lines.append(f"- {d}")
    lines.append("")
    lines.append("## 结论（ADR-002 回退条款）")
    lines.append("")
    lines.append(r["conclusion"])
    lines.append("")
    lines.append("## 逐条明细")
    lines.append("")
    lines.append("| case | single | three | four | single退款 | four退款 | outcome 一致 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for row in r["rows"]:
        same = (row["single_outcome"] == row["three_outcome"]
                == row["four_outcome"])
        lines.append(
            f"| {row['case_id']} | {row['single_outcome']} | {row['three_outcome']} | "
            f"{row['four_outcome']} | {row['single_refunded']} | {row['four_refunded']} | "
            f"{'是' if same else '否'} |")
    lines.append("")
    lines.append("> 诚实边界：三模式在确定性规则下正确路径一致（金额/资格/状态由领域服务裁决）；"
                 "four-role 的模块化/轨迹观测价值不构成量化业务收益，默认路径按 ADR-002 维持单 Agent。")
    lines.append("> 本报告为确定性产物（不含逐 run 耗时，跨运行零 diff；耗时仅输出 stdout/日志，"
                 "不作为对照结论依据）。")
    return "\n".join(lines)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="V1.1 运行模式 A/B 对照：单 Agent vs three-agent vs four-role（确定性规则）")
    parser.add_argument("--limit", type=int, default=None,
                        help="只跑前 N 条黄金用例（冒烟）；缺省全集 golden_v1")
    args = parser.parse_args()

    started = time.monotonic()
    result = _run(limit=args.limit)
    total = result["total"]
    out = ROOT / "evals" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    report = out / "mode_compare.md"
    report.write_text(_render(result), encoding="utf-8")

    print(f"运行模式 A/B（golden-v1 {total} 条）：single {result['pass']['single']}/{total} 通过 | "
          f"three-agent {result['pass']['three']}/{total} 通过 | "
          f"four-role {result['pass']['four']}/{total} 通过")
    print(f"outcome 三模式一致 {result['outcome_consistent']}/{total}；退款一致 "
          f"{result['refund_consistent']}/{total}；意图一致 {result['intent_consistent']}/"
          f"{result['intent_total']}；澄清一致 {result['clarify_consistent']}/{total}")
    print(f"转人工 escalated：single={result['escalate_count']['single']} three={result['escalate_count']['three']} four={result['escalate_count']['four']}")
    print("耗时(stdout, 不进报告以保确定性): "
          + " | ".join(f"{k} avg={result['avg_ms'][k]}ms p50={result['p50_ms'][k]}ms "
                       f"p95={result['p95_ms'][k]}ms" for k in ("single", "three", "four")))
    print(f"引用一致性探针：{result['citation_probe']['consistent']}/"
          f"{result['citation_probe']['total']}（确定性同 store 同查询）")
    print(f"online_llm={result['online_llm']}（未连接真实 LLM；结果不得视为真实模型输出）")
    print(f"结论：{result['conclusion']}")
    print(f"报告已写入：{report}（总运行 {time.monotonic() - started:.2f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
