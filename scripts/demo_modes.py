"""Agent 运行模式开关冒烟（V1.1 阶段 3.6）：single_agent / multi_agent / offline_rule / llm。

运行（项目根，D 盘 .venv）：
    .venv\\Scripts\\python.exe scripts\\demo_modes.py

展示（真实执行，每步断言）：
  1) RuntimeMode 四值与默认（single_agent，README 已明确）；
  2) resolve_mode("llm") 在未配置安全 Key / 白名单 Base URL 时解析为 offline_rule
     （不静默直连任何真实模型）；
  3) 以 single_agent 模式跑一条正常破损退款（确定性规则，领域裁决金额/审批）。

说明：本脚本不连接真实 LLM；"llm" 仅在显式配置 OPSPILOT_LLM_API_KEY + 白名单
OPSPILOT_LLM_BASE_URL 后可用（本环境未配置 → 如实回落 offline_rule，未实测）。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agents import WorkflowRunner  # noqa: E402
from src.agents.modes import RuntimeMode, resolve_mode  # noqa: E402
from src.domain.after_sales import (  # noqa: E402
    AfterSalesService,
    Order,
    OrderItem,
    OrderStatus,
    PolicyRule,
    RequestType,
)
from src.domain.after_sales.adapters import MemoryAdapter  # noqa: E402

BAR = "=" * 70


def _svc() -> AfterSalesService:
    svc = AfterSalesService()
    svc.seed_order(Order(
        order_id="ORD-1", tenant_id="T1", customer_id="C1",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("100.00"),
        items=[OrderItem(sku="SKU-1", name="演示", quantity=1, unit_price=Decimal("100.00"))],
        days_since_sign=2,
    ))
    svc.seed_policy(PolicyRule(
        policy_id="P-DAMAGED-FULL", tenant_id="T1", request_type=RequestType.REFUND,
        reason_tags=("damaged",), window_days=30, refund_ratio=Decimal("1.00"),
    ))
    return svc


def main() -> int:
    print("OpsPilot Agent 运行模式冒烟（确定性规则，真实执行）")
    print(f"\n{BAR}\n[1] 运行模式值与默认\n{BAR}")
    for m in RuntimeMode:
        print(f"  {m.value}")
    default = RuntimeMode.default()
    assert default is RuntimeMode.SINGLE_AGENT
    print(f"  默认 = {default.value}（README 已明确；多 Agent A/B 无收益、真实 LLM 未接入）")

    print(f"\n{BAR}\n[2] resolve_mode：llm 未配置 → offline_rule（不静默直连）\n{BAR}")
    for requested in ("single_agent", "multi_agent", "offline_rule", "llm"):
        actual = resolve_mode(requested)
        note = "" if actual.value == requested else f"  -> 回落 {actual.value}"
        print(f"  {requested}{note}")
    assert resolve_mode("llm") is RuntimeMode.OFFLINE_RULE, "无 Key 时 llm 必须回落离线规则"
    assert resolve_mode("single_agent") is RuntimeMode.SINGLE_AGENT

    print(f"\n{BAR}\n[3] single_agent 模式：正常破损退款闭环\n{BAR}")
    svc = _svc()
    runner = WorkflowRunner(MemoryAdapter(svc))
    r = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="mode-smoke")
    assert r.waiting_approval
    op_id = r.state["operation_id"]
    print(f"  草稿金额 {r.state['action_draft']['amount']} 元（领域政策计算）→ interrupt 等审批")
    runner.submit_decision(op_id, "approved")
    final = runner.resume("mode-smoke")
    assert final.outcome == "refunded"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    print(f"  outcome={final.outcome}，退款={svc.refunded_amount('ORD-1')} 元（领域裁决）")

    print(f"\n{BAR}\n冒烟通过：四种模式可解析；默认 single_agent；llm 未配置安全回落离线。\n{BAR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
