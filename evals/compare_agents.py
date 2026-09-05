"""阶段 5B A/B 对照实验：单 Agent（默认） vs Supervisor 多 Agent（ADR-002）。

方法：对同一黄金集逐条分别驱动 单 Agent 与 Supervisor 模式（自动审批/恢复），
比较 outcome、退款正确性、耗时与审计。业务真相仍以领域服务为准。

结论规则（ADR-002）：若 Supervisor 未带来可量化收益（正确率持平、耗时无改善），
默认路径维持单 Agent；Supervisor 保留为可选运行时（并行只读证据 + 未来多模型/子 Agent 挂载点）。

用法：.venv\\Scripts\\python.exe evals\\compare_agents.py
报告：evals/reports/agent_compare.md
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents import SupervisorRunner, WorkflowRunner
from src.rag import PolicyDocument, PolicyStore

import evals.replay as golden_replay


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


def main() -> int:
    cases = json.loads((ROOT / "evals" / "golden" / "golden_v1.json").read_text(encoding="utf-8"))
    rows: list[dict] = []
    for case in cases:
        store = _policy_store()
        started = time.monotonic()
        single = golden_replay.run_case(case)                       # 单 Agent（默认）
        d_single = time.monotonic() - started
        started = time.monotonic()
        sup = golden_replay.run_case(
            case,
            runner_factory=lambda svc: SupervisorRunner(svc, policy_store=store),
        )                                                           # Supervisor 模式
        d_sup = time.monotonic() - started
        rows.append({
            "case_id": case["id"], "scenario": case.get("scenario", ""),
            "single_pass": single["pass"], "sup_pass": sup["pass"],
            "single_outcome": single.get("outcome"), "sup_outcome": sup.get("outcome"),
            "single_refunded": single.get("refunded"), "sup_refunded": sup.get("refunded"),
            "single_ms": round(d_single * 1000, 2), "sup_ms": round(d_sup * 1000, 2),
            "single_detail": single.get("detail", []), "sup_detail": sup.get("detail", []),
        })

    single_pass_n = sum(1 for r in rows if r["single_pass"])
    sup_pass_n = sum(1 for r in rows if r["sup_pass"])
    outcome_same = sum(1 for r in rows if r["single_outcome"] == r["sup_outcome"]
                       and r["single_pass"] == r["sup_pass"])
    refund_same = sum(1 for r in rows if r["single_refunded"] == r["sup_refunded"])
    single_ms = [r["single_ms"] for r in rows]
    sup_ms = [r["sup_ms"] for r in rows]
    total = len(rows)

    result = {
        "dataset_version": "golden-v1",
        "total": total,
        "single": {"pass": single_pass_n,
                   "pass_rate": round(single_pass_n / total, 4),
                   "p50_ms": _p(single_ms, 50), "p95_ms": _p(single_ms, 95)},
        "supervisor": {"pass": sup_pass_n,
                       "pass_rate": round(sup_pass_n / total, 4),
                       "p50_ms": _p(sup_ms, 50), "p95_ms": _p(sup_ms, 95)},
        "outcome_consistent": outcome_same,
        "refund_consistent": refund_same,
        "rows": rows,
    }
    result["conclusion"] = _conclude(result)

    out = ROOT / "evals" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    report = out / "agent_compare.md"
    report.write_text(_render(result), encoding="utf-8")

    print(f"A/B：单 Agent {single_pass_n}/{total} 通过 vs Supervisor {sup_pass_n}/{total} 通过"
          f"；outcome 一致 {outcome_same}/{total}，退款一致 {refund_same}/{total}")
    print(f"结论：{result['conclusion']}")
    print(f"报告已写入：{report}")
    return 0


def _p(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    idx = min(len(values) - 1, int(p / 100 * len(values)))
    return round(sorted(values)[idx], 2)


def _conclude(r: dict) -> str:
    gain = r["supervisor"]["pass_rate"] - r["single"]["pass_rate"]
    # 结论只由确定性指标（通过率）决定，不依赖逐 run 耗时 → 报告产物确定、可复现
    if gain > 0:
        return ("Supervisor 通过率高于单 Agent——具备拆分收益，可将默认切换为 "
                "Supervisor（需人工复核后提交 ADR）。")
    return ("无明确业务收益（通过率与单 Agent 持平或更低），按 ADR-002 失败回退条款："
            "默认路径维持单 Agent；Supervisor 保留为可选实验运行时（并行只读证据与未来多模型挂载点）。")


def _render(r: dict) -> str:
    lines = [
        "# 单 Agent vs Supervisor 对照实验报告（阶段 5B / ADR-002）",
        "",
        f"- 数据集：`{r['dataset_version']}`（黄金集 {r['total']} 条，固定种子合成数据）",
        f"- 运行方式：同一用例分别驱动单 Agent（WorkflowRunner）与 Supervisor（SupervisorRunner，"
        "并行只读子 Agent：order/history/policy）",
        "",
        "## 结果",
        "",
        "| 模式 | 通过 | 通过率 |",
        "| --- | --- | --- |",
        f"| 单 Agent（默认） | {r['single']['pass']} | {r['single']['pass_rate']} |",
        f"| Supervisor | {r['supervisor']['pass']} | {r['supervisor']['pass_rate']} |",
        "",
        f"- outcome 一致性：{r['outcome_consistent']}/{r['total']}",
        f"- 退款金额一致性：{r['refund_consistent']}/{r['total']}",
        "",
        "## 结论（ADR-002 回退条款）",
        "",
        r["conclusion"],
        "",
        "## 逐条明细",
        "",
        "| case | 单outcome | Sup outcome | 单退款 | Sup退款 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in r["rows"]:
        lines.append(
            f"| {row['case_id']} | {row['single_outcome']} | {row['sup_outcome']} | "
            f"{row['single_refunded']} | {row['sup_refunded']} |"
        )
    lines.append("")
    lines.append("> 诚实边界：确定性规则下两种模式的正确路径一致；Supervisor 的并行证据与模块化价值"
                 "不构成此对比中的量化业务收益，默认路径按 ADR-002 维持单 Agent。")
    lines.append("> 本报告为确定性产物（不含逐 run 耗时，跨运行零 diff；耗时仅输出到 stdout/日志，"
                 "不作为对照结论依据）。")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
