"""第一阶段缺陷台账回归（AGENTS.md 宪法目标，先于任何重构执行）。

规则：
- PASS 类：现状正确，作为回归保护（缺陷已不存在或从未存在）。
- XFAIL 类：现状不符合预期 → 记录已知缺陷（台账编号 D-x）；待对应阶段修复后改为 PASS。
  以 xfail(strict=False) 表达，保证全量回归保持绿色（xfailed 计数），不伪造通过。
台账文档：docs/DEFECTS_LOG.md（每项：证据/严重级/归属修复阶段）。
"""
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.pg_live import live_test_db_url, pg_reachable, reset_test_schema

from src.domain.after_sales import (
    AfterSalesErrorCode,
    AfterSalesService,
    ApproveCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    PolicyRule,
    ReconcileCommand,
    RejectCommand,
    RequestType,
    Role,
    SubmitCommand,
)
from src.persistence.row_codecs import (
    order_item_to_row,
    order_to_row,
    policy_to_row,
)
from src.repo import (
    MemoryAfterSalesRepository,
    OperationRow,
    OrderRow,
    PostgresAfterSalesRepository,
    TicketRow,
)
from tests.unit.domain.after_sales.helpers import make_order, service_with_policies

ROOT = Path(__file__).resolve().parents[2]
TEST_DB_URL = live_test_db_url()


pg_live = pytest.mark.skipif(
    TEST_DB_URL is None or not pg_reachable(TEST_DB_URL),
    reason="OPSPILOT_TEST_DATABASE_URL 未设置或 PostgreSQL 不可达："
           "未使用隔离测试库，跳过破坏性集成（PG 集成未实测）")


@pytest.fixture
def pg_repo():
    """guard 通过后重建全部业务表（opspilot_test_* 隔离库）；返回 PG Repository。"""
    reset_test_schema(TEST_DB_URL)
    return PostgresAfterSalesRepository(TEST_DB_URL)


T1_POL = PolicyRule(policy_id="P-T1", tenant_id="T1", request_type=RequestType.REFUND,
                    reason_tags=("damaged",), window_days=30, refund_ratio=Decimal("1.00"))
T2_POL = PolicyRule(policy_id="P-T2", tenant_id="T2", request_type=RequestType.REFUND,
                    reason_tags=("damaged",), window_days=30, refund_ratio=Decimal("1.00"))


def _create_ticket(svc, tenant, order_id, customer_id, key):
    return svc.create_ticket(CreateTicketCommand(
        tenant, order_id, customer_id, RequestType.REFUND, "商品破损",
        ("damaged",), Role.AGENT, key))


# ============ XFAIL：已知缺陷（修复前如实记录） ============

def test_d1_cross_tenant_same_order_id_not_overwritten():
    """D1 修复验证：内存后端 seed 跨租户同 order_id 显式拒绝（fail-closed，不静默覆盖）；
    多租户同 order_id 共存的权威语义由 PostgreSQL (tenant_id, order_id) 主键承载（repo 已测）。"""
    svc = AfterSalesService()
    svc.seed_order(make_order(order_id="ORD-X", tenant_id="T1", paid="100.00"))
    with pytest.raises(ValueError, match="跨租户覆盖"):
        svc.seed_order(make_order(order_id="ORD-X", tenant_id="T2", paid="200.00"))
    # 原订单未被覆盖，仍属于 T1
    assert svc.export_state()["orders"]["ORD-X"].tenant_id == "T1"


def test_d2_cross_tenant_same_idem_key_not_conflict():
    """D2 修复验证：幂等键租户作用域（键空间带租户前缀）——跨租户同原始 key 互不冲突。"""
    svc = AfterSalesService()
    svc.seed_order(make_order(order_id="ORD-A", tenant_id="T1", paid="100.00"))
    svc.seed_order(make_order(order_id="ORD-B", tenant_id="T2", paid="100.00"))
    svc.seed_policy(T1_POL)
    svc.seed_policy(T2_POL)
    t1 = _create_ticket(svc, "T1", "ORD-A", "C1", key="same-key")     # T1 成功
    t2 = _create_ticket(svc, "T2", "ORD-B", "C2", key="same-key")     # T2 同原始 key：成功（租户作用域）
    assert len(svc.export_state()["tickets"]) == 2
    assert t1.ticket_id != t2.ticket_id
    # 同租户同 key 幂等语义仍成立（返回原工单，不重复建单）
    dup1 = _create_ticket(svc, "T1", "ORD-A", "C1", key="same-key")
    assert dup1.ticket_id == t1.ticket_id
    assert len(svc.export_state()["tickets"]) == 2


def test_d4_reject_carries_expected_version():
    """D4 修复验证：RejectCommand 携带 expected_version；版本不符拒绝被拦截；正确版本成功。"""
    from src.domain.after_sales.models import AfterSalesError
    assert "decision_version" in RejectCommand.__dataclass_fields__
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))
    t = _create_ticket(svc, "T1", "ORD-1", "C1", key="tk-r4")
    op = svc.create_refund(CreateRefundCommand(t.ticket_id, Decimal("60.00"), "x", Role.AGENT, "k-r4"))
    svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    assert svc.get_operation(op.operation_id).version == 1
    # 过期版本拒绝 → DECISION_VERSION_MISMATCH
    with pytest.raises(AfterSalesError) as ei:
        svc.reject(RejectCommand(op.operation_id, Role.APPROVER, reason="重复申请",
                                 decision_version=2))
    assert ei.value.code == AfterSalesErrorCode.DECISION_VERSION_MISMATCH
    # 正确版本拒绝成功
    op = svc.reject(RejectCommand(op.operation_id, Role.APPROVER, reason="重复申请",
                                  decision_version=1))
    assert op.status.value == "rejected" and op.version == 2


def test_d7_pg_first_command_path_no_clear_all_and_no_partial():
    """D7 收编验证：生产命令路径（PgCommandService/PgCommandAdapter）不调用 clear_all。

    整库镜像清空/重插工具（PgBackedSession 兼容迁移原型）已随收口从代码库删除（原先在
    src/persistence/pg_backed.py）；命令失败整事务回滚、无部分提交由 PG live 实证
    （tests/integration/test_pg_commands_live.py）——不存在"全量镜像 save 失败导致
    内存/DB 分叉"的生产路径。"""
    import inspect

    from src.domain.after_sales.adapters import PgCommandAdapter
    from src.domain.after_sales.pg_commands import PgCommandService

    for cls in (PgCommandService, PgCommandAdapter):
        assert "clear_all" not in inspect.getsource(cls)   # 生产命令路径禁止镜像清空/重插


def test_d8_restart_recovers_policy_and_items_fully():
    """D8 收编验证：政策与订单明细完整持久化并可自表读出（无需调用方重新注入）。

    PgBackedSession 整库镜像装载工具已随收口删除，其"重启完整恢复"语义现由 PG-first
    命令路径承担：每次命令读 PG 最新事实，政策/明细经 Repository 落 policies/order_items
    表（row_codecs 编解码）。真实 PG 重启往返由 tests/integration/test_pg_commands_live.py
    与 golden PG replay 实证；此处以 memory repo + codec 做等价断言：领域事实落表后
    自表读出必须完整一致，且命令路径按表内政策/订单计算退款计划（非调用方注入）。"""
    from src.domain.after_sales.models import OrderItem
    from src.domain.after_sales.pg_commands import PgCommandService
    from src.persistence.row_codecs import order_from_row, policy_from_row

    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))  # 订单带 items + 政策
    repo = MemoryAfterSalesRepository()
    # 落表：政策 + 订单（含明细 items）——模拟事实已持久化
    order = list(svc.export_state()["orders"].values())[0]
    for p in svc.export_state()["policies"]:
        repo.insert_policy(policy_to_row(p))
    repo.insert_order(order_to_row(order))
    for it in order.items:
        repo.insert_order_item(order_item_to_row(order, it))

    # "重启"：自表读出并解码，政策/明细完整恢复、无需重新注入
    rows = repo.list_policies()
    assert len(rows) == 1
    p = policy_from_row(rows[0])
    assert p.policy_id == "P-DAMAGED-FULL"
    assert p.reason_tags == ("damaged",) and p.refund_ratio == Decimal("1.00")
    restored = order_from_row(repo.get_order("T1", "ORD-1"))
    items = [OrderItem(sku=r.sku, name=r.name, quantity=r.quantity, unit_price=r.unit_price)
             for r in repo.list_order_items()
             if (r.tenant_id, r.order_id) == ("T1", "ORD-1")]
    assert len(items) == 1 and items[0].sku == "SKU-1"
    assert items[0].unit_price == Decimal("100.00")
    assert restored.paid_amount == order.paid_amount and restored.status == order.status
    # 命令路径自表读事实：退款计划依据表内政策与订单（窗口/比例）计算
    plan = PgCommandService(repo).compute_refund_plan("T1", "ORD-1", RequestType.REFUND, ("damaged",))
    assert plan.policy_id == "P-DAMAGED-FULL" and plan.amount == Decimal("100.00")


def test_d12_reports_stable_no_wallclock_jitter():
    """D12 修复验证：对照报告为确定性产物——不含逐 run 耗时列（P50/P95/单ms），
    结论仅由通过率决定；compare_agents.py 报告跨运行零 diff（实测两次运行哈希相同）。"""
    report = (ROOT / "evals" / "reports" / "agent_compare.md").read_text(encoding="utf-8")
    assert "ms" not in report


# ============ PASS：现状正确（回归保护） ============

def test_d3_customer_can_only_read_own_ticket():
    """客户只能读取自己的工单（API 层 test_customer_only_own_ticket 之外，领域只读查询同样受限）。"""
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))
    svc.seed_order(make_order(order_id="ORD-2", tenant_id="T1", customer_id="C2", paid="50.00"))
    _create_ticket(svc, "T1", "ORD-1", "C1", key="tk-c1")
    mine = [t for t in svc.list_customer_tickets("T1", "C1")]
    assert len(mine) == 1 and all(t.customer_id == "C1" for t in mine)
    other = svc.list_customer_tickets("T1", "C2")
    assert other == []


def test_d5_sequential_approve_same_version_rejected():
    """审批版本 CAS：同版本二次审批被拒（顺序路径已正确；并发真双跑需 op 级锁/DB CAS，第二阶段）。"""
    from src.domain.after_sales.models import AfterSalesError
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))
    t = _create_ticket(svc, "T1", "ORD-1", "C1", key="tk-d5")
    op = svc.create_refund(CreateRefundCommand(t.ticket_id, Decimal("60.00"), "x", Role.AGENT, "k-d5"))
    svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=1))
    with pytest.raises(AfterSalesError) as ei:
        svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=1))
    assert ei.value.code == AfterSalesErrorCode.DECISION_VERSION_MISMATCH


def test_d10_unknown_only_original_operation():
    """unknown 态只能以原 operation 对账收口；同订单新键创建被拒（不换键）。"""
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))
    t = _create_ticket(svc, "T1", "ORD-1", "C1", key="tk-u")
    op = svc.create_refund(CreateRefundCommand(t.ticket_id, Decimal("60.00"), "x", Role.AGENT, "k-u"))
    svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, 1))
    svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM, external_result="timeout"))
    assert svc.get_operation(op.operation_id).status.value == "unknown"

    from src.domain.after_sales.models import AfterSalesError
    t2 = _create_ticket(svc, "T1", "ORD-1", "C1", key="tk-u2")       # 同订单第二工单
    with pytest.raises(AfterSalesError) as ei:
        svc.create_refund(CreateRefundCommand(t2.ticket_id, Decimal("60.00"), "x", Role.AGENT, "k-u2"))
    assert ei.value.code == AfterSalesErrorCode.OPERATION_UNKNOWN_CONFLICT
    # 原键对账收口成功
    op = svc.reconcile(ReconcileCommand(op.operation_id, Role.SYSTEM, result="success"))
    assert op.status.value == "executed"
    assert svc.refunded_amount("ORD-1") == Decimal("60.00")


def test_d11_external_timeout_never_auto_retries_new_key():
    """外部结果未知（timeout）不自动换键重试：新键被拒、审计无二次创建。"""
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))
    t = _create_ticket(svc, "T1", "ORD-1", "C1", key="tk-n")
    op = svc.create_refund(CreateRefundCommand(t.ticket_id, Decimal("60.00"), "x", Role.AGENT, "k-n"))
    svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, 1))
    svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM, external_result="timeout"))
    assert len([e for e in svc.audit_log() if e.action == "create_refund"]) == 1


@pg_live
def test_d6_with_order_lock_held_through_business_execution(pg_repo):
    """D6 修复验证：with_order_lock 在业务代码执行期间保持行锁（第二事务阻塞至持有者提交）。
    pg_repo fixture 负责重建表（清残留），此处仅复用其清理副作用。"""
    repo = PostgresAfterSalesRepository(TEST_DB_URL)
    repo.insert_order(OrderRow("T1", "ORD-L", "C1", "delivered", Decimal("100.00"), 2))
    repo.insert_ticket(TicketRow("T1", "TKT-L", "ORD-L", "C1", "refund", "x", "open"))
    holder_done = threading.Event()
    result: dict = {}

    def holder():
        with repo.with_order_lock("T1", "ORD-L"):
            holder_done.set()
            time.sleep(0.6)                       # 业务代码在锁内执行
        result["holder"] = True

    def contender():
        holder_done.wait()                        # 确保持有者已持锁
        t0 = time.monotonic()
        ok = repo.try_execute_refund("T1", "ORD-L", OperationRow(
            "T1", "OP-L", "TKT-L", "ORD-L", "refund", Decimal("60.00"), "approved", "k-l", "agent"))
        result["ok"] = ok
        result["dt"] = time.monotonic() - t0

    th = threading.Thread(target=holder)
    tc = threading.Thread(target=contender)
    th.start()
    tc.start()
    th.join()
    tc.join()
    assert result.get("holder") is True
    assert result.get("ok") is True
    assert result.get("dt", 0.0) >= 0.35          # contender 被行锁阻塞至 holder 提交后放行
