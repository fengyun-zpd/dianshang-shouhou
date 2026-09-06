"""OpsPilot 面试 5 分钟演示脚本（确定性规则单 Agent，真实执行）。

运行（项目根，D 盘 .venv）：
    .venv\\Scripts\\python.exe scripts\\demo_interview.py

固化五核心场景（与黄金集 golden_v1 同构；全部为固定种子合成数据，每步断言）：
  1) 正常闭环：破损退款 → 意图 → 证据 → 确定性退款计划 → 草稿 → 人工审批 interrupt →
     approve → resume 重读领域决定 → 执行 → 关单审计；
  2) 信息不足：缺订单号 → 澄清 interrupt（不猜测金额/政策）；
  3) 无政策证据：诉求无适用政策 → 领域 POLICY_NOT_FOUND → 转人工，不虚构政策/金额；
  4) 审批拒绝：授权人 reject → resume 后零副作用收尾（不执行、不退款）；
  5) 外部结果未知：外部超时 → operation_unknown（金额 0）→ 仅以原 operation_id
     对账 success → 落账。

补充安全演示（同一脚本，面试按需展示）：
  6) 跨租户访问：T2 会话读 T1 订单 → 拒绝转人工，零副作用；
  7) 重复请求：同线程同请求再次 start → 返回原结果，不重复退款。

演示结论（每步断言，任意失败即抛 AssertionError）：
  金额/资格/状态/幂等/审批一律由确定性领域服务裁决；Agent 只检索证据与生成草稿。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agents import WorkflowRunner
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


def _build_service() -> AfterSalesService:
    """合成固定种子：T1 订单 ORD-1（实付 100.00）+ 破损全额政策；T2 无此订单。"""
    svc = AfterSalesService()
    svc.seed_order(Order(
        order_id="ORD-1", tenant_id="T1", customer_id="C1",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("100.00"),
        items=[OrderItem(sku="SKU-1", name="演示商品", quantity=1,
                         unit_price=Decimal("100.00"))],
        days_since_sign=2,
    ))
    svc.seed_policy(PolicyRule(
        policy_id="P-DAMAGED-FULL", tenant_id="T1", request_type=RequestType.REFUND,
        reason_tags=("damaged",), window_days=30, refund_ratio=Decimal("1.00"),
    ))
    return svc


def _build_service_no_policy() -> AfterSalesService:
    """合成固定种子：T1 订单 ORD-1（实付 100.00），但**无任何适用 PolicyRule**。"""
    svc = AfterSalesService()
    svc.seed_order(Order(
        order_id="ORD-1", tenant_id="T1", customer_id="C1",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("100.00"),
        items=[OrderItem(sku="SKU-1", name="演示商品", quantity=1,
                         unit_price=Decimal("100.00"))],
        days_since_sign=2,
    ))
    return svc


def _new_runner(svc) -> WorkflowRunner:
    return WorkflowRunner(MemoryAdapter(svc))


def demo1_happy_refund(svc) -> None:
    print(f"\n{BAR}\n[演示 1] 正常破损退款（人工审批 → 恢复 → 执行 → 关单）\n{BAR}")
    runner = _new_runner(svc)
    r = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="i1")
    assert r.waiting_approval
    op_id = r.state["operation_id"]
    print(f"  证据引用      : {[x for x in r.state['evidence_refs'] if x.startswith(('order', 'customer', 'history'))]}")
    print(f"  草稿金额      : {r.state['action_draft']['amount']} 元（确定性领域政策计算）")
    print(f"  领域操作状态  : {svc.get_operation(op_id).status.value}（已 interrupt 等待人工审批）")
    print("  授权人员提交【通过】→ resume")
    runner.submit_decision(op_id, "approved")
    final = runner.resume("i1")
    assert final.outcome == "refunded"
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    t = svc.get_ticket(r.state["ticket_id"])
    print(f"  结果          : outcome={final.outcome}，退款={svc.refunded_amount('ORD-1')} 元，"
          f"工单={t.status.value}/{t.resolution}，审计 {len(svc.audit_log())} 条")


def demo_no_policy_escalates_without_fabrication() -> None:
    print(f"\n{BAR}\n[演示 3] 无政策证据 → 领域 POLICY_NOT_FOUND → 转人工（不虚构政策/金额）\n{BAR}")
    svc = _build_service_no_policy()
    runner = _new_runner(svc)
    r = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="i3nopolicy")
    assert r.finished and r.outcome == "escalated"
    st = r.state or {}
    assert r.error_code == "AFTER_SALES_POLICY_NOT_FOUND"
    assert not st.get("action_draft")                # 无草稿：不虚构政策/金额
    assert svc.audit_log() == [] and svc.refunded_amount("ORD-1") == Decimal("0.00")
    print(f"  结果          : outcome={r.outcome}，error={r.error_code}")
    print(f"  reply         : {r.reply}")
    print("  零副作用（无草稿、无审计、无退款）：Agent 未猜测政策或金额")


def demo_approval_rejected_zero_side_effect() -> None:
    print(f"\n{BAR}\n[演示 4] 审批拒绝 → resume 后零副作用收尾（不执行、不退款）\n{BAR}")
    svc = _build_service()
    runner = _new_runner(svc)
    r = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="i4reject")
    assert r.waiting_approval
    op_id = r.state["operation_id"]
    runner.submit_decision(op_id, "rejected", reason="重复申请")
    final = runner.resume("i4reject")
    assert final.outcome == "rejected"
    op = svc.get_operation(op_id)
    t = svc.get_ticket(r.state["ticket_id"])
    print(f"  结果          : outcome={final.outcome}；操作状态={op.status.value}（拒绝）")
    print(f"  工单          : {t.status.value}/{t.resolution}；退款累计={svc.refunded_amount('ORD-1')}（零执行）")
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    assert len([e for e in svc.audit_log() if e.action == "execute"]) == 0
    print("  零副作用：无 execute 审计、无退款，审批决定以领域服务为准")


def demo2_missing_order_clarify() -> None:
    print(f"\n{BAR}\n[演示 2] 缺订单号 → 澄清 interrupt（不猜测）\n{BAR}")
    svc = _build_service()
    runner = _new_runner(svc)
    r = runner.start("T1", "我要退款", thread_id="i2")
    assert r.waiting_clarify, "缺订单号应进入澄清中断"
    assert svc.audit_log() == [] and svc.refunded_amount("ORD-1") == Decimal("0.00")
    print(f"  澄清问题      : {r.interrupt_value.get('questions')}")
    print("  补充订单号 → resume")
    r2 = runner.resume("i2", payload={"order_id": "ORD-1", "description": "订单 ORD-1 商品破损，要求退款"})
    assert r2.waiting_approval
    print(f"  补参后进入人工审批（草稿金额 {r2.state['action_draft']['amount']} 元），未猜测金额")


def demo3_cross_tenant_rejected() -> None:
    print(f"\n{BAR}\n[演示 8] 跨租户访问（T2 会话读 T1 订单）→ 拒绝转人工，零副作用\n{BAR}")
    svc = _build_service()
    runner = _new_runner(svc)
    r = runner.start("T2", "订单 ORD-1 商品破损，要求退款", thread_id="i3")
    assert r.outcome == "escalated", r
    print(f"  结果          : outcome={r.outcome}（T2 看不到 T1 的 ORD-1）")
    print(f"  reply         : {r.reply}")
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    print("  零业务副作用（无退款、无审计）")


def demo4_repeat_request_idempotent() -> None:
    print(f"\n{BAR}\n[演示 9] 重复请求（同线程同请求再次 start）→ 原结果，不重复退款\n{BAR}")
    svc = _build_service()
    runner = _new_runner(svc)
    r1 = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="i4")
    op_id = r1.state["operation_id"]
    runner.submit_decision(op_id, "approved")
    f1 = runner.resume("i4")
    assert f1.outcome == "refunded"
    first_refunded = svc.refunded_amount("ORD-1")
    # 同请求再次 start（模拟用户/系统重复提交）
    r2 = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="i4")
    print(f"  首次 outcome   : {f1.outcome}（退款 {first_refunded}）")
    print(f"  重复提交 outcome: {r2.outcome}")
    assert svc.refunded_amount("ORD-1") == first_refunded == Decimal("100.00")
    assert len([e for e in svc.audit_log() if e.action == "execute"]) == 1
    print("  无重复副作用：execute 审计仅 1 条，退款仍 100.00")


def demo5_unknown_reconcile_original_key() -> None:
    print(f"\n{BAR}\n[演示 5] operation_unknown：外部超时 → unknown（金额 0）→ 原键对账 success → 落账\n{BAR}")
    svc = _build_service()
    runner = _new_runner(svc)
    r = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="i5",
                     simulate_external="timeout")
    op_id = r.state["operation_id"]
    runner.submit_decision(op_id, "approved")
    r2 = runner.resume("i5")
    assert r2.outcome == "operation_unknown"
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
    print(f"  审批通过但外部超时 → outcome={r2.outcome}，退款累计 {svc.refunded_amount('ORD-1')}（未盲目入账）")
    print("  仅以原 operation_id 对账（success）")
    runner.reconcile_unknown(op_id, "success")
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")
    op = svc.get_operation(op_id)
    print(f"  对账后         : 操作 {op.status.value}，退款 {svc.refunded_amount('ORD-1')} 元（原键对账成功）")


def main() -> None:
    svc = _build_service()
    print("OpsPilot 面试演示：确定性规则单 Agent（合成数据 seed，真实执行）")
    print("\n—— 核心五场景（正常闭环 / 信息不足 / 无政策证据 / 审批拒绝 / 外部结果未知）——")
    demo1_happy_refund(svc)                        # 1 正常闭环
    demo2_missing_order_clarify()                  # 2 信息不足（缺订单号澄清）
    demo_no_policy_escalates_without_fabrication() # 3 无政策证据 → 转人工
    demo_approval_rejected_zero_side_effect()      # 4 审批拒绝 → 零副作用
    demo5_unknown_reconcile_original_key()         # 5 外部结果未知 → 原键对账
    print("\n—— 补充安全演示（跨租户拒绝 / 重复请求幂等）——")
    demo3_cross_tenant_rejected()
    demo4_repeat_request_idempotent()
    print(f"\n{BAR}\n全部 7 个场景通过（含任务卡五核心场景）：金额/资格/状态/幂等/审批均由确定性"
          f"领域服务裁决；Agent 只检索证据与生成草稿。\n{BAR}")


if __name__ == "__main__":
    main()
