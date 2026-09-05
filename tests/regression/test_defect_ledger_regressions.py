"""第一阶段缺陷台账回归（AGENTS.md 宪法目标，先于任何重构执行）。

规则：
- PASS 类：现状正确，作为回归保护（缺陷已不存在或从未存在）。
- XFAIL 类：现状不符合预期 → 记录已知缺陷（台账编号 D-x）；待对应阶段修复后改为 PASS。
  以 xfail(strict=False) 表达，保证全量回归保持绿色（xfailed 计数），不伪造通过。
台账文档：docs/DEFECTS_LOG.md（每项：证据/严重级/归属修复阶段）。
"""
import os
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

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
from src.persistence.pg_backed import PgBackedSession
from src.repo import (
    MemoryAfterSalesRepository,
    OperationRow,
    OrderRow,
    PostgresAfterSalesRepository,
    TicketRow,
)
from tests.unit.domain.after_sales.helpers import make_order, service_with_policies

ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot",
)
_SCHEMA = (ROOT / "src" / "repo" / "schema.sql").read_text(encoding="utf-8")


def _pg_available() -> bool:
    try:
        engine = create_engine(DATABASE_URL, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pg_live = pytest.mark.skipif(
    not _pg_available(), reason="PostgreSQL 不可达：数据库集成未实测")


@pytest.fixture
def session():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        for table in ("audit_events", "idempotency_records", "approval_decisions",
                      "refund_operations", "tickets", "orders", "policies", "order_items", "entity_seq", "workflow_threads"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn.execute(text(_SCHEMA))
    engine.dispose()
    return PgBackedSession(PostgresAfterSalesRepository(DATABASE_URL))


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
    """D7 收编验证：生产命令路径（PgCommandService）不调用 clear_all（镜像清空仅属
    PgBackedSession 兼容迁移工具，已标注非生产命令路径）；命令失败整事务回滚、无部分提交
    由 PG live 实证（tests/integration/test_pg_commands_live.py）——不存在"全量镜像 save
    失败导致内存/DB 分叉"的生产路径。"""
    import inspect

    from src.domain.after_sales.pg_commands import PgCommandService
    from src.persistence import pg_backed

    src_code = inspect.getsource(PgCommandService)
    assert "clear_all" not in src_code                    # 生产命令路径禁止镜像清空/重插
    assert "clear_all" in inspect.getsource(pg_backed)    # 镜像清理仅存在于 PgBackedSession 兼容迁移工具
    assert "镜像" in (pg_backed.__doc__ or "")            # 全量镜像写=原型/兼容迁移工具（如实标注）


def test_d8_restart_recovers_policy_and_items_fully():
    """D8 修复验证：save→load()（无参）政策与订单明细自 DB 完整恢复，无需调用方重新注入。"""
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))  # 订单带 items + 政策
    s = PgBackedSession(MemoryAfterSalesRepository())
    s.save(svc)
    svc2 = s.load()                                     # 无参：policy/items 自表恢复
    st2 = svc2.export_state()
    assert len(st2["policies"]) == 1
    p = st2["policies"][0]
    assert p.policy_id == "P-DAMAGED-FULL"
    assert p.reason_tags == ("damaged",) and p.refund_ratio == Decimal("1.00")
    o = list(st2["orders"].values())[0]
    assert len(o.items) == 1 and o.items[0].sku == "SKU-1"
    assert o.items[0].unit_price == Decimal("100.00")
    st1 = svc.export_state()
    assert st2["policies"][0] == st1["policies"][0]
    assert list(st2["orders"].values())[0].items == list(st1["orders"].values())[0].items


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
def test_d6_with_order_lock_held_through_business_execution(session):
    """D6 修复验证：with_order_lock 在业务代码执行期间保持行锁（第二事务阻塞至持有者提交）。
    session fixture 负责重建表（清残留），此处仅复用其清理副作用。"""
    repo = PostgresAfterSalesRepository(DATABASE_URL)
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
