"""审计关联（R3）：`audit_event_ids` 必须按租户 + 线程 + 本线程实体重建。

缺陷背景（`docs/V1_2_REVIEW_2026-09-13.md` R3）：网关原先用单一全局水位，
既会漏掉他租户线程的事件，也会把同租户另一线程的审批事件混入本线程。

覆盖：
- T1 → T2 → T1 交错时，他租户线程的实体事件不得出现在本线程；
- 同租户两个线程交错审批时，他线程的审批事件不得混入；
- 重复 state / 重复 decision / 已终态再恢复都不重复累计编号；
- 新运行器实例（共享 checkpointer）重建后编号一致；
- 编号来自领域审计事实（可重建），进程内集合只是去重视图。
"""
from __future__ import annotations

from decimal import Decimal

from langgraph.checkpoint.memory import MemorySaver

from src.agents import WorkflowRunner
from src.domain.after_sales import (
    Order,
    OrderItem,
    OrderStatus,
    PolicyRule,
    RequestType,
)
from src.domain.after_sales.adapters import MemoryAdapter
from tests.unit.agents.helpers import REQUEST_DAMAGED
from tests.unit.domain.after_sales.helpers import service_with_policies


def _two_orders_one_tenant() -> object:
    """T1 两条订单 + 兜底政策（用于同租户两线程交错）。"""
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))
    svc.seed_order(Order(
        order_id="ORD-2", tenant_id="T1", customer_id="C1",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("100.00"),
        items=[OrderItem(sku="SKU-2", name="x", quantity=1, unit_price=Decimal("100.00"))],
        days_since_sign=1,
    ))
    return svc


def _two_tenants() -> object:
    """T1/T2 各一条订单与政策（用于跨租户交错）。"""
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))
    svc.seed_order(Order(
        order_id="ORD-T2", tenant_id="T2", customer_id="C9",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("100.00"),
        items=[OrderItem(sku="SKU-T2", name="x", quantity=1, unit_price=Decimal("100.00"))],
        days_since_sign=1,
    ))
    svc.seed_policy(PolicyRule(
        policy_id="P-T2-DAMAGED", tenant_id="T2", request_type=RequestType.REFUND,
        reason_tags=("damaged",), window_days=30, refund_ratio=Decimal("1.00"),
    ))
    return svc


def _entities(ids: list[str]) -> set[str]:
    return {item.rsplit(":", 1)[-1] for item in ids}


# ---------- 跨租户交错：T1 → T2 → T1 ----------

def test_audit_ids_stay_scoped_across_test_tenants():
    svc = _two_tenants()
    runner = WorkflowRunner(MemoryAdapter(svc))

    t1 = runner.start("T1", REQUEST_DAMAGED, thread_id="audit-t1")
    t2 = runner.start("T2", "订单 ORD-T2 商品破损，要求退款", thread_id="audit-t2",
                      order_id_hint="ORD-T2")
    # 回到 T1：他租户线程存在期间，本线程仍只看到自己的事件
    t1_again = runner.get_state("audit-t1", tenant_id="T1")

    assert t1.state["ticket_id"] != t2.state["ticket_id"]
    assert len(t1.audit_event_ids) == len(set(t1.audit_event_ids))
    assert len(t2.audit_event_ids) == len(set(t2.audit_event_ids))
    # 两个线程各自都有建单/草稿/提交三件事实（旧实现会漏掉后启动租户的 3 条）
    assert len(t1.audit_event_ids) == 3
    assert len(t2.audit_event_ids) == 3
    # 他租户实体绝不混入
    t2_entities = _entities(t2.audit_event_ids)
    assert not (_entities(t1.audit_event_ids) & t2_entities)
    assert t2.state["ticket_id"] not in " ".join(t1.audit_event_ids)
    assert t1.state["ticket_id"] not in " ".join(t2.audit_event_ids)
    # 重读只读视图不改变关联结果
    assert t1_again.audit_event_ids == t1.audit_event_ids


# ---------- 同租户两线程交错审批 ----------

def test_same_tenant_threads_do_not_mix_approval_events():
    svc = _two_orders_one_tenant()
    runner = WorkflowRunner(MemoryAdapter(svc))

    a = runner.start("T1", REQUEST_DAMAGED, thread_id="audit-a")
    b = runner.start("T1", "订单 ORD-2 商品破损，要求退款", thread_id="audit-b",
                     order_id_hint="ORD-2")
    op_a, op_b = a.state["operation_id"], b.state["operation_id"]
    assert op_a != op_b

    # 先审批 B（后启动的线程），再恢复 A：旧实现会把 B 的审批事件记到 A 上
    runner.submit_decision(op_b, "approved", tenant_id="T1")
    done_b = runner.resume("audit-b", tenant_id="T1")
    runner.submit_decision(op_a, "approved", tenant_id="T1")
    done_a = runner.resume("audit-a", tenant_id="T1")

    assert done_a.outcome == "refunded" and done_b.outcome == "refunded"
    assert op_b not in " ".join(done_a.audit_event_ids), "B 的审批事件不得混入 A"
    assert op_a not in " ".join(done_b.audit_event_ids), "A 的审批事件不得混入 B"
    assert any(f":approve:{op_a}" in i for i in done_a.audit_event_ids)
    assert any(f":execute:{op_a}" in i for i in done_a.audit_event_ids)
    assert _entities(done_a.audit_event_ids) == {a.state["ticket_id"], op_a}


# ---------- 重复读取 / 重复恢复不重复累计 ----------

def test_repeated_read_and_resume_do_not_duplicate_audit_ids():
    svc = _two_orders_one_tenant()
    runner = WorkflowRunner(MemoryAdapter(svc))
    started = runner.start("T1", REQUEST_DAMAGED, thread_id="audit-dup")
    op_id = started.state["operation_id"]

    for _ in range(3):
        assert runner.get_state("audit-dup", tenant_id="T1").audit_event_ids == \
            started.audit_event_ids

    runner.submit_decision(op_id, "approved", tenant_id="T1")
    done = runner.resume("audit-dup", tenant_id="T1")
    assert len(done.audit_event_ids) == len(set(done.audit_event_ids)), "编号不得重复"

    # 已终态再恢复：不重复累计、也不新增业务副作用
    audit_actions = [e.action for e in svc.audit_log()]
    for _ in range(2):
        again = runner.resume("audit-dup", tenant_id="T1")
        assert again.audit_event_ids == done.audit_event_ids
    assert [e.action for e in svc.audit_log()] == audit_actions
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


# ---------- 新实例重建 ----------

def test_audit_ids_rebuild_identically_in_new_runner_instance():
    """共享 checkpointer 的新实例读取同一线程，审计关联必须一致（不依赖进程内水位）。"""
    svc = _two_orders_one_tenant()
    checkpointer = MemorySaver()
    runner1 = WorkflowRunner(MemoryAdapter(svc), checkpointer=checkpointer)
    started = runner1.start("T1", REQUEST_DAMAGED, thread_id="audit-rebuild")

    runner2 = WorkflowRunner(MemoryAdapter(svc), checkpointer=checkpointer)
    view = runner2.get_state("audit-rebuild", tenant_id="T1")
    assert view.audit_event_ids == started.audit_event_ids

    runner2.submit_decision(started.state["operation_id"], "approved", tenant_id="T1")
    done = runner2.resume("audit-rebuild", tenant_id="T1")
    assert done.outcome == "refunded"
    assert len(done.audit_event_ids) == len(set(done.audit_event_ids))
    assert any(":execute:" in i for i in done.audit_event_ids)


def test_audit_ids_come_from_domain_facts_not_process_state():
    """审计编号必须与领域审计事实逐条对应（稳定 index+action+entity），而非进程内计数。"""
    svc = _two_orders_one_tenant()
    runner = WorkflowRunner(MemoryAdapter(svc))
    started = runner.start("T1", REQUEST_DAMAGED, thread_id="audit-source")

    owned = {started.state["ticket_id"], started.state["operation_id"]}
    log = svc.audit_log("T1")
    expected = {f"{event.event_id or f'audit-{index}'}:{event.action}:{event.entity_id}"
                for index, event in enumerate(log) if event.entity_id in owned}

    assert set(started.audit_event_ids) == expected, "编号必须可由领域审计事实重建"
    assert _entities(started.audit_event_ids) <= {e.entity_id for e in log}
    # 他线程/他实体的事件不在其中
    assert all(item.rsplit(":", 1)[-1] in owned for item in started.audit_event_ids)
    assert all(event.event_id for event in log if event.entity_id in owned)
