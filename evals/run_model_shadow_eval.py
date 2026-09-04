"""阶段 5A 影子评测入口：模型预测与离线规则 / 黄金集对照。

用法（项目根，D 盘 .venv）：
    .venv\\Scripts\\python.exe evals\\run_model_shadow_eval.py --mode offline
    .venv\\Scripts\\python.exe evals\\run_model_shadow_eval.py --mode candidate

影子原则：
- 正常业务继续使用离线规则；候选模型只对同一输入做预测；
- 预测不触碰领域服务 / 数据库 / 审批（零业务副作用）；
- candidate 必须显式配置安全环境（OPSPILOT_LLM_API_KEY / BASE_URL 白名单）：
  未配置时安全降级——不联网，报告明确写"未实测"。

报告：evals/reports/shadow_eval_<mode>.md（含模型/Prompt/数据集版本、耗时、Token、成本、错误率与诚实边界）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.models import (
    IntentExtraction,
    ModelConfigError,
    ModelError,
    ModelGateway,
    ModelInvocationMetadata,
    load_llm_settings,
)

DATASET_VERSION = "golden-v1"
PROMPT_VERSION = "1.0"


def load_cases() -> list[dict]:
    file = ROOT / "evals" / "golden" / "golden_v1.json"
    return json.loads(file.read_text(encoding="utf-8"))


def _prediction_to_dict(payload: IntentExtraction) -> dict:
    return {
        "intent": payload.intent.value,
        "order_id": payload.order_id,
        "reason_tags": list(payload.reason_tags),
        "missing_fields": list(payload.missing_fields),
        "confidence": payload.confidence,
    }


def run(mode: str, limit: Optional[int] = None, outdir: Optional[Path] = None) -> dict:
    """执行影子评测并返回结构化结果。mode: offline | candidate。"""
    cases = load_cases()
    if limit:
        cases = cases[:limit]
    started = time.monotonic()

    gateway, notes = _build_gateway(mode)
    rows: list[dict] = []
    for case in cases:
        text = case["request"]
        try:
            resp = gateway.analyze_intent(text, dataset_version=DATASET_VERSION)
            payload, meta = resp.payload, resp.metadata
            pred = _prediction_to_dict(payload)
            error = None
        except ModelError as e:
            pred, meta = {}, ModelInvocationMetadata(
                provider="error", model_name="error", task="intent_classification",
                prompt_version=PROMPT_VERSION, duration_ms=0.0, degraded=True,
                error=type(e).__name__,
            )
            error = type(e).__name__

        expected_intent = case.get("expected", {}).get("intent")
        intent_ok = (expected_intent is None) or (pred.get("intent") == expected_intent)
        # 澄清识别：该输入在离线规则下是否需要澄清（缺参非空）
        needs_clarify = bool(pred.get("missing_fields"))
        rows.append({
            "case_id": case["id"], "scenario": case.get("scenario", ""),
            "provider": meta.provider, "model": meta.model_name,
            "degraded": meta.degraded, "error": error or meta.error,
            "prediction": pred, "expected_intent": expected_intent,
            "intent_ok": intent_ok, "needs_clarify": needs_clarify,
            "duration_ms": meta.duration_ms, "input_tokens": meta.input_tokens,
            "output_tokens": meta.output_tokens, "cost_usd": meta.cost_estimate_usd,
        })

    total = len(rows)
    intent_ok_n = sum(1 for r in rows if r["expected_intent"] is not None and r["intent_ok"])
    intent_denom = sum(1 for r in rows if r["expected_intent"] is not None)
    errors = [r for r in rows if r["error"]]
    degraded = [r for r in rows if r["degraded"]]
    durations = sorted(r["duration_ms"] for r in rows)
    tokens = sum(r["input_tokens"] + r["output_tokens"] for r in rows)
    costs = sum(r["cost_usd"] for r in rows)
    elapsed = time.monotonic() - started

    result = {
        "mode": mode,
        "dataset_version": DATASET_VERSION,
        "prompt_version": PROMPT_VERSION,
        "model_name": gateway._primary.model_name if (mode == "candidate" and gateway.has_primary_model) else
                      (gateway._offline.model_name if mode == "offline" else "offline/rules-v1"),
        "model_version": gateway._primary.model_version if (mode == "candidate" and gateway.has_primary_model) else "1.0",
        "provider": "openai-compatible" if (mode == "candidate" and gateway.has_primary_model) else "offline-rule",
        "elapsed_seconds": round(elapsed, 3),
        "total": total,
        "intent_accuracy": round(intent_ok_n / intent_denom, 4) if intent_denom else None,
        "clarify_detected": sum(1 for r in rows if r["needs_clarify"]),
        "error_count": len(errors),
        "error_rate": round(len(errors) / total, 4) if total else 0.0,
        "degraded_count": len(degraded),
        "p50_ms": _percentile(durations, 50), "p95_ms": _percentile(durations, 95),
        "total_tokens": tokens, "total_cost_usd": round(costs, 6),
        "notes": notes,
        "business_side_effect": "0（影子模式仅文本预测，未调用任何领域写路径）",
        "rows": rows,
    }

    out = outdir or (ROOT / "evals" / "reports")
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / f"shadow_eval_{mode}.md"
    report_path.write_text(_render(result), encoding="utf-8")
    result["report_path"] = str(report_path)

    print(_summary(result))
    return result


def _build_gateway(mode: str):
    if mode == "offline":
        return ModelGateway(), ["离线规则基线（不联网、零成本）"]
    if mode == "candidate":
        settings = load_llm_settings()
        try:
            gateway = ModelGateway(settings=settings)
        except ModelConfigError as e:
            # 安全降级：未配置/白名单外 → 不联网，以离线对照运行并明确标注"未实测"
            gateway = ModelGateway()
            return gateway, [f"candidate 主模型未启用（安全失败）：{e}。未联网；以下为主模型“未实测”的离线对照。"]
        return gateway, [f"主模型已启用：{settings.model}（provider=openai-compatible）。"]
    raise ValueError(f"未知 mode：{mode}（仅 offline / candidate）")


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(p / 100 * len(sorted_values)))
    return sorted_values[idx]


def _summary(r: dict) -> str:
    unmeasured = "（未实测）" if (r["mode"] == "candidate" and r["provider"] == "offline-rule") else ""
    return (
        f"影子评测[{r['mode']}]{unmeasured}：模型={r['model_name']}@{r['model_version']} "
        f"dataset={r['dataset_version']} prompt={r['prompt_version']} "
        f"意图准确率={r['intent_accuracy']} 错误率={r['error_rate']} 降级数={r['degraded_count']} "
        f"P95={r['p95_ms']}ms tokens={r['total_tokens']} cost=${r['total_cost_usd']} "
        f"报告={r['report_path']}"
    )


def _render(r: dict) -> str:
    lines = [
        "# 影子评测报告（阶段 5A）",
        "",
        f"- 模式：`{r['mode']}`",
        f"- 模型：`{r['model_name']}`（版本 `{r['model_version']}`，provider `{r['provider']}`）",
        f"- Prompt 版本：`{r['prompt_version']}`｜数据集版本：`{r['dataset_version']}`",
        f"- 运行耗时：{r['elapsed_seconds']} s",
        "",
        "## 指标",
        "",
        f"- 用例数：{r['total']}",
        f"- 意图准确率：{r['intent_accuracy']}",
        f"- 识别需澄清数：{r['clarify_detected']}",
        f"- 错误数：{r['error_count']}（错误率 {r['error_rate']}）",
        f"- 降级数：{r['degraded_count']}",
        f"- 耗时：P50 {r['p50_ms']} ms / P95 {r['p95_ms']} ms",
        f"- Token：{r['total_tokens']}｜成本：${r['total_cost_usd']}",
        f"- 业务副作用：{r['business_side_effect']}",
        "",
        "## 说明",
        "",
    ]
    lines += [f"- {n}" for n in r["notes"]]
    if r["mode"] == "candidate" and r["provider"] == "offline-rule":
        lines.append("- 候选模型未实测（未配置安全 Key 或 Base URL 白名单校验失败，未联网）；"
                     "以上为离线规则对照，不代表候选模型指标。")
    lines += ["", "## 逐条明细", ""]
    lines.append("| case | provider | intent(预测/期望) | 命中 | degraded | error | tokens | 耗时ms |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in r["rows"]:
        exp = row["expected_intent"] or "-"
        pred_i = row["prediction"].get("intent", "-")
        lines.append(
            f"| {row['case_id']} | {row['provider']} | {pred_i}/{exp} | "
            f"{'✅' if row['intent_ok'] else '❌'} | {row['degraded']} | "
            f"{row['error'] or '-'} | {row['input_tokens'] + row['output_tokens']} | {row['duration_ms']} |"
        )
    lines.append("")
    lines.append("> 诚实边界：全部为固定随机种子合成数据；Token 为估算值；真实模型必须实际运行后回填。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 5A 影子评测")
    parser.add_argument("--mode", choices=["offline", "candidate"], required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--outdir", type=Path, default=None)
    args = parser.parse_args()
    run(mode=args.mode, limit=args.limit, outdir=args.outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
