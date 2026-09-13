"""LLM-as-Judge 评测 CLI 入口。

用法（项目根，D 盘 .venv）：
    .venv\\Scripts\\python.exe evals\\run_llm_judge.py --mode offline
    .venv\\Scripts\\python.exe evals\\run_llm_judge.py --mode judge

- offline：恒可运行、不联网、零成本，报告写 evals/reports/judge_offline.md；
- judge：需同时满足「Key + 白名单 Base URL + 显式模型名」才启用真实裁判；否则安全降级为
  离线并联网请求数为 0，报告写 evals/reports/judge_candidate.md，并明确写出「真实 Judge
  未实测；当前结果为离线规则裁判；未产生网络请求」。即使启用了真实裁判，只要没有一条
  未降级的真实调用结果，报告仍按未实测输出（不夸大）。

评测样本来自项目内黄金集 evals/golden/golden_v1.json；每条构造成一个 JudgeCase，
output_text 用离线规则基线生成的解释/澄清话术（确定性，不联网、不触发领域写操作）。
Judge 只评语言质量，绝不评金额/权限/审批/状态/幂等/副作用——业务结论由确定性验收负责。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import llm_judge  # noqa: E402


def load_golden_cases() -> list[dict]:
    """读取项目内黄金集（list[dict]，含 id/request/expected）。"""
    file = ROOT / "evals" / "golden" / "golden_v1.json"
    return json.loads(file.read_text(encoding="utf-8"))


def _offline_reply(case: dict) -> str:
    """从黄金集期望结果生成确定性的离线解释/澄清话术（供 Judge 评测）。"""
    outcome = (case.get("expected") or {}).get("outcome")
    err = (case.get("expected") or {}).get("error_code")
    if outcome == "clarify":
        return "请问您需要退款的订单号是多少？请提供订单号以便核实。"
    if outcome == "escalated":
        if err == "AFTER_SALES_ORDER_NOT_FOUND":
            return "经核对未找到该订单，已转人工处理；具体金额、权限与审批由系统裁决。"
        if err == "AFTER_SALES_POLICY_NOT_FOUND":
            return "当前缺少适用政策证据，已转人工处理；金额、权限与审批由系统裁决。"
        if err == "AFTER_SALES_POLICY_CONFLICT":
            return "存在冲突政策，已转人工处理；金额、权限与审批由系统裁决。"
        return "该请求超出系统能力，已转人工处理；金额、权限与审批由系统裁决。"
    # refunded / rejected / operation_unknown / 其它：说明处理方向并披露边界（含政策引用）
    return "已依据政策证据（P-DAMAGED-FULL）说明处理方向；具体金额、权限与审批由确定性系统裁决，不迁移状态。"


def _build_cases(cases: list[dict], limit: Optional[int]) -> list[llm_judge.JudgeCase]:
    out: list[llm_judge.JudgeCase] = []
    for c in (cases[:limit] if limit else cases):
        expected = c.get("expected") or {}
        outcome = expected.get("outcome")
        if outcome == "refunded":
            refs = ["P-DAMAGED-FULL"]
            summary = "说明处理方向"
        elif outcome == "clarify":
            refs = None
            summary = "提供订单号"
        elif outcome == "escalated":
            refs = None
            summary = "转人工"
        else:
            refs = None
            summary = "说明处理方向"
        out.append(llm_judge.JudgeCase(
            case_id=c["id"],
            output_text=_offline_reply(c),
            expected_summary=summary,
            evidence_refs=refs,
            # 黄金集未内嵌确定性业务结论字段；本 CLI 不重跑确定性验收 → business_ok=None（不伪造）
            business_ok=None,
            outcome=outcome,
            error_code=expected.get("error_code"),
        ))
    return out


def _summary(r: dict) -> str:
    agg = r["aggregate"]
    dims = " ".join(f"{k}={v}" for k, v in agg["dimension_mean"].items())
    marker = "实测" if r["measured"] else "未实测"
    return (
        f"LLM-Judge[{r['mode']}]：样本={r['total']} 裁判={r['judge_model']} "
        f"prompt={r['prompt_version']} 模式={r['mode']} 是否实测={marker} "
        f"（真实裁判启用={r['judge_enabled']}，未降级={r['measured_cases']}，"
        f"网络请求={r['network_requests']}）"
        f" 各维度均分=[{dims}] 降级数={r['degraded_count']} 报告={r['report_path']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM-as-Judge 评测（默认离线规则裁判）")
    parser.add_argument("--mode", choices=["offline", "judge"], default="offline")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--outdir", type=Path, default=None)
    args = parser.parse_args()
    cases = _build_cases(load_golden_cases(), args.limit)
    r = llm_judge.run_judge(cases, mode=args.mode, outdir=args.outdir)
    print(_summary(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
