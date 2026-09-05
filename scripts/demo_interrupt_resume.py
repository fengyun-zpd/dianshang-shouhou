"""演示：单 Agent 售后工作流的 interrupt / resume（人工审批中断与恢复）。

运行（D 盘虚拟环境，项目根目录）：
    .venv\\Scripts\\python.exe scripts\\demo_interrupt_resume.py

展示：
1) 用户请求 → 意图识别 → 只读证据 → 确定性退款计划 → 草稿落库；
2) 高风险动作进入人工审批 interrupt（工作流挂起）；
3) 授权人员（模拟）向领域事实源提交"通过/拒绝"决定；
4) resume 后工作流重读领域决定：通过 → 受控执行 → 关单审计；拒绝 → 零副作用收尾。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agents import WorkflowRunner
from src.domain.after_sales import OperationStatus, RequestType, Role, TicketStatus
from src.domain.after_sales import AfterSalesService, CreateTicketCommand, Order, OrderItem, OrderStatus
from src.domain.after_sales import PolicyRule
from src.domain.after_sales.adapters import MemoryAdapter


def build_demo_service() -> AfterSalesService:
    """固定种子合成数据：订单 ORD-1001 实付 200.00 + 破损全额政策。"""
    svc = AfterSalesService()
    svc.seed_order(Order(
        order_id="ORD-1001", tenant_id="T1", customer_id="C1",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("200.00"),
        items=[OrderItem(sku="SKU-9", name="演示商品", quantity=1, unit_price=Decimal("200.00"))],
        days_since_sign=1,
    ))
    svc.seed_policy(PolicyRule(
        policy_id="P-DEMO", tenant_id="T1", request_type=RequestType.REFUND,
        reason_tags=("damaged",), window_days=30, refund_ratio=Decimal("1.00"),
    ))
    return svc


def main() -> None:
    svc = build_demo_service()
    runner = WorkflowRunner(MemoryAdapter(svc))

    print("=" * 64)
    print("演示 1：审批通过路径（interrupt → 决定 → resume → 执行）")
    print("=" * 64)
    r1 = runner.start("T1", "订单 ORD-1001 商品破损，要求退款", thread_id="demo-approve")
    assert r1.waiting_approval, "应在人工审批处 interrupt"
    print(f"[1] 工作流已 interrupt，等待审批")
    print(f"    审批编号 : {r1.state['approval_id']}")
    print(f"    操作编号 : {r1.state['operation_id']}  （与审批编号独立）")
    print(f"    草稿金额 : {r1.state['action_draft']['amount']} 元（领域政策计算）")
    print(f"    证据引用 : {r1.state['evidence_refs']}")
    op1 = svc.get_operation(r1.state["operation_id"])
    print(f"[2] 草稿已落库，领域状态 = {op1.status.value}（PENDING_APPROVAL）")

    print("\n[3] 授权人员提交【通过】决定到领域服务…")
    runner.submit_decision(r1.state["operation_id"], "approved")
    r1b = runner.resume("demo-approve")
    assert r1b.finished and r1b.outcome == "refunded"
    print(f"[4] resume 完成：outcome={r1b.outcome}")
    print(f"    实际退款 = {svc.refunded_amount('ORD-1001')} 元")
    print(f"    工单状态 = {svc.get_ticket(r1.state['ticket_id']).status.value}（resolution="
          f"{svc.get_ticket(r1.state['ticket_id']).resolution}）")
    print(f"    审计事件 = {len(svc.audit_log())} 条")

    print("\n" + "=" * 64)
    print("演示 2：审批拒绝路径（resume 后零副作用）")
    print("=" * 64)
    svc2 = build_demo_service()   # 独立服务，避免演示 1 已全额退款的干扰
    runner2 = WorkflowRunner(MemoryAdapter(svc2))
    r2 = runner2.start("T1", "订单 ORD-1001 商品破损，要求退款", thread_id="demo-reject")
    assert r2.waiting_approval
    runner2.submit_decision(r2.state["operation_id"], "rejected", reason="重复申请")
    r2b = runner2.resume("demo-reject")
    assert r2b.outcome == "rejected"
    op2 = svc2.get_operation(r2.state["operation_id"])
    print(f"[1] outcome={r2b.outcome}；操作状态={op2.status.value}")
    print(f"    累计退款仍 = {svc2.refunded_amount('ORD-1001')} 元（拒绝未执行任何副作用）")

    print("\n" + "=" * 64)
    print("演示 3：伪造审批恢复被拒绝（决定以领域服务为准）")
    print("=" * 64)
    svc3 = build_demo_service()   # 独立服务
    runner3 = WorkflowRunner(MemoryAdapter(svc3))
    r3 = runner3.start("T1", "订单 ORD-1001 商品破损，要求退款", thread_id="demo-forge")
    assert r3.waiting_approval
    r3b = runner3.resume("demo-forge", payload="approved")  # 伪造：未提交真实决定
    print(f"[1] 伪造 resume('approved') 后仍在等待真实审批: waiting={r3b.waiting_approval}")
    print(f"    领域操作状态 = {svc3.get_operation(r3.state['operation_id']).status.value}"
          f"（未被伪造决定推进），退款累计 = {svc3.refunded_amount('ORD-1001')}")
    runner3.submit_decision(r3.state["operation_id"], "approved")
    r3c = runner3.resume("demo-forge")
    print(f"[2] 真实决定提交并 resume 后：outcome={r3c.outcome}")

    print("\n演示完成：interrupt/resume 真实执行，审批决定始终重读领域事实源。")


if __name__ == "__main__":
    main()
