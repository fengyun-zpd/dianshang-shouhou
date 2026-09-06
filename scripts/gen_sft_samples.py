"""微调实验入口：固定种子合成 SFT 样本生成器（V1.1 阶段五，骨架）。

边界（诚实声明）：
- **本机无 GPU、未配置真实模型**：本脚本只生成"意图分类 / 澄清 / 工具参数"三类
  合成样本（固定随机种子 42，输出到 D 盘 .runtime 或 .cache 下），**不执行任何训练**；
  是否微调（LoRA/SFT）须先满足 AGENTS.md 第八条与 docs/MODEL_EVALUATION.md §6 门禁
  （基线稳定、有对照、数据可校验），且实际运行后才能声称任何效果；
- 不把动态价格/库存/订单/政策生效状态/客户画像写入样本（它们必须来自 DB/检索）；
- 没有高质量 chosen/rejected 偏好数据 → 不实现 DPO；
- 微调模型若未来启用，必须受与基础模型相同的权限/工具/内容安全约束
  （金额/审批/状态/执行指令由 assert_output_safe 拦截）。

用法（D 盘 .venv，项目根）：
    .venv\\Scripts\\python.exe scripts\\gen_sft_samples.py --out .runtime/sft/golden_sft.jsonl

样本 schema（每行 JSON）：
    {"task": "intent|clarify|tool_args", "prompt": str, "target": str, "seed": int}
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SEED = 42
_ORDERS = ("ORD-1", "ORD-2", "ORD-1001")
_REASONS = (
    ("商品破损，要求退款", ("damaged",)),
    ("少件漏发，怎么处理", ("missing_item",)),
    ("收到就坏了，能换吗", ("quality",)),
    ("不想要了想退货", ("return",)),
)
_VERBS = ("怎么申请", "可以退款吗", "帮我处理一下", "要求处理")


def _make_prompt(order: str, req: str, verb: str) -> str:
    return f"订单 {order} {req}，{verb}"


def generate(n_per_task: int = 40) -> list[dict]:
    rng = random.Random(SEED)
    rows: list[dict] = []

    # 1) 意图分类样本：request → intent 标签
    for i in range(n_per_task):
        order = rng.choice(_ORDERS)
        req, tags = _REASONS[i % len(_REASONS)]
        verb = rng.choice(_VERBS)
        intent = "refund" if tags[0] in ("damaged", "missing_item", "quality") else "return"
        rows.append({
            "task": "intent", "seed": SEED,
            "prompt": _make_prompt(order, req, verb),
            "target": json.dumps({"intent": intent, "reason_tags": list(tags)},
                                 ensure_ascii=False),
        })

    # 2) 澄清样本：缺订单号/缺诉求 → 要澄清的字段
    clarify_fields = ("order_id", "description")
    for i in range(n_per_task):
        missing = [clarify_fields[i % len(clarify_fields)]]
        text = "我要退款" if "order_id" in missing else "订单 ORD-1 处理一下"
        rows.append({
            "task": "clarify", "seed": SEED,
            "prompt": text,
            "target": json.dumps({"missing_fields": missing}, ensure_ascii=False),
        })

    # 3) 工具参数样本：请求 → 应调用的只读工具参数（仅证据检索，无写）
    for i in range(n_per_task):
        order = _ORDERS[i % len(_ORDERS)]
        tool = "get_order" if i % 2 == 0 else "retrieve_policy"
        args = {"order_id": order} if tool == "get_order" else {
            "query": "破损退款政策", "top_k": 3}
        rows.append({
            "task": "tool_args", "seed": SEED,
            "prompt": f"订单 {order} 商品破损，查一下",
            "target": json.dumps({"tool": tool, "args": args}, ensure_ascii=False),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="合成 SFT 样本生成器（骨架，不训练）")
    parser.add_argument("--out", default=str(ROOT / ".runtime" / "sft" / "golden_sft.jsonl"))
    parser.add_argument("--per-task", type=int, default=40)
    args = parser.parse_args()

    rows = generate(args.per_task)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_task: dict[str, int] = {}
    for r in rows:
        by_task[r["task"]] = by_task.get(r["task"], 0) + 1
    print(f"已生成 {len(rows)} 条合成样本 -> {out}")
    print(f"分任务：{by_task}")
    print("说明：本脚本只生成数据，未执行任何训练（本机无 GPU/真实模型）。"
          "训练与效果需满足 MODEL_EVALUATION §6 门禁并实际运行后才可声称。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
