"""确定性黄金集生成器（K6）：固定随机种子（42），生成 120 条售后场景用例。

输出：evals/golden/golden_v2.json。全部合成数据，不代表真实业务。
分类（与领域/Agent 实际语义一致）：
  0 破损退款-审批通过(refunded) / 1 破损退款-审批拒绝(rejected)
  2 破损退款-超时→unknown→对账成功 / 3 缺订单号→澄清(clarify)
  4 少件-无适用政策→转人工(POLICY_NOT_FOUND) / 5 换货→转人工(intent exchange)
  6 问候/无法识别→转人工 / 7 破损-冲突政策→转人工(POLICY_CONFLICT)
另有 repeat / forged 特例插入。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "evals" / "golden" / "golden_v2.json"

_DAMAGED = ["订单 ORD-1 商品破损，要求退款", "订单 ORD-1 收到的东西坏了，要退钱",
            "订单 ORD-1 质量破损想退掉"]
_NO_ORDER = ["商品破损，要求退款", "东西坏了要退钱"]
_NO_POLICY = ["订单 ORD-1 少件漏发，要求退款", "订单 ORD-1 收到后发现缺件，要求退款"]
_EXCHANGE = ["订单 ORD-1 我想换货", "订单 ORD-1 麻烦换一个"]
_UNKNOWN = ["你好", "请问在吗"]


def main() -> None:
    rng = random.Random(42)
    cases: list[dict] = []
    specials = 0
    for i in range(120):
        variant = i % 8
        case_id = f"g2-{i + 1:04d}"
        case: dict = {"id": case_id, "tenant": "T1"}
        if variant == 0:
            case.update({"scenario": "refund-approved",
                         "request": rng.choice(_DAMAGED), "approval": "approved"})
            case["expected"] = {"outcome": "refunded", "refunded": "100.00", "intent": "refund"}
        elif variant == 1:
            case.update({"scenario": "refund-rejected",
                         "request": rng.choice(_DAMAGED), "approval": "rejected"})
            case["expected"] = {"outcome": "rejected", "refunded": "0.00", "intent": "refund"}
        elif variant == 2:
            case.update({"scenario": "refund-unknown-reconcile",
                         "request": rng.choice(_DAMAGED), "approval": "approved",
                         "simulate_external": "timeout", "reconcile_after_unknown": "success"})
            case["expected"] = {"outcome": "operation_unknown", "refunded": "0.00",
                                "refunded_after_reconcile": "100.00", "intent": "refund"}
        elif variant == 3:
            case.update({"scenario": "clarify-missing-order", "request": rng.choice(_NO_ORDER)})
            case["expected"] = {"outcome": "clarify", "refunded": "0.00"}
        elif variant == 4:
            case.update({"scenario": "no-applicable-policy", "request": rng.choice(_NO_POLICY)})
            case["expected"] = {"outcome": "escalated", "refunded": "0.00",
                                "error_code": "AFTER_SALES_POLICY_NOT_FOUND", "intent": "refund"}
        elif variant == 5:
            case.update({"scenario": "exchange-escalated", "request": rng.choice(_EXCHANGE)})
            case["expected"] = {"outcome": "escalated", "refunded": "0.00", "intent": "exchange"}
        elif variant == 6:
            case.update({"scenario": "unknown-intent", "request": rng.choice(_UNKNOWN)})
            case["expected"] = {"outcome": "escalated", "refunded": "0.00", "intent": "unknown"}
        else:  # 7: 冲突政策
            case.update({"scenario": "conflict-policy", "request": rng.choice(_DAMAGED),
                         "extra_policies": [["P-DAMAGED-HALF", ["damaged"], "0.50", 30]]})
            case["expected"] = {"outcome": "escalated", "refunded": "0.00",
                                "error_code": "AFTER_SALES_POLICY_CONFLICT", "intent": "refund"}
        # 每 40 条穿插 repeat 与 forged 特例（保持确定性，覆盖不变式）
        if i % 40 == 20:
            case.update({"scenario": "repeat-request", "request": _DAMAGED[0],
                         "approval": "approved", "repeat_same_thread": True})
            case["expected"] = {"outcome": "refunded", "repeat_outcome": "refunded",
                                "refunded": "100.00", "intent": "refund"}
        if i % 40 == 30:
            case.update({"scenario": "forged-resume-safe", "request": _DAMAGED[0],
                         "approval": "approved", "forged_resume_first": True})
            case["expected"] = {"outcome": "refunded", "refunded": "100.00",
                                "forged_was_ignored": True, "intent": "refund"}
        cases.append(case)

    # 用固定但乱序的 id 序列（可复现）
    ids = list(range(len(cases)))
    rng.shuffle(ids)
    ordered = [cases[idx] for idx in ids]
    for idx, c in enumerate(ordered):
        c["id"] = f"g2-{idx + 1:04d}"
    OUT.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"generated {len(ordered)} cases -> {OUT}")


if __name__ == "__main__":
    main()
