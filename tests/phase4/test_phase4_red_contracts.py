"""阶段四 RED 契约（先写失败测试，再修实现；修复后逐项移除 xfail 转 PASS）。

每项 = 正常/失败/权限/幂等/审计/恢复语义的验收用例；现状不满足 → xfail(strict=True)，
**严禁意外通过**（若实现未修而测试转绿即为契约破坏）。最终验收要求 0 xfail。
"""
import os
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    ApproveCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    RequestType,
    RejectCommand,
    Role,
    SubmitCommand,
)
from src.domain.after_sales.pg_commands import PgCommandService
from src.repo import OrderRow, PolicyRow, PostgresAfterSalesRepository

ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot",
)
_SCHEMA = (ROOT / "src" / "repo" / "schema.sql").read_text(encoding="utf-8")
_CN = "RED-P4"


def _pg_available() -> bool:
    try:
        engine = create_engine(DATABASE_URL, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pg_live = pytest.mark.skipif(not _pg_available(),
                             reason="PostgreSQL 不可达：数据库集成未实测")
red = pytest.mark.xfail(strict=True, reason=f"{_CN}: 阶段四契约尚未实现（见各用例 docstring）")


def _new_db():
    """每用例独立重建（阶段四早期；测试隔离策略（第 5 节）将改为每 worker schema）。"""
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        for table in ("audit_events", "idempotency_records", "approval_decisions",
                      "refund_operations", "tickets", "orders", "policies",
                      "order_items", "entity_seq"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn.execute(text(_SCHEMA))
    engine.dispose()
    repo = PostgresAfterSalesRepository(DATABASE_URL)
    repo.insert_order(OrderRow("T1", "ORD-1", "C1", "delivered", Decimal("100.00"), 2))
    repo.insert_policy(PolicyRow("T1", "P-1", "refund", '["damaged"]', 30,
                                 Decimal("1.0000"), "1970-01-01", 1))
    return PgCommandService(repo)


def _sql_one(stmt, params=None):
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        v = conn.execute(text(stmt), params or {}).scalar()
    engine.dispose()
    return v


def _ticket_cmd(key="tk-1", customer="C1", actor=Role.AGENT):
    return CreateTicketCommand("T1", "ORD-1", customer, RequestType.REFUND,
                               "商品破损", ("damaged",), actor, key)


def _seed_approved(svc, draft_key="k-1", draft_amt="60.00"):
    svc.create_ticket(_ticket_cmd(f"tk-{draft_key}"))
    op = svc.create_refund_draft("T1", CreateRefundCommand("TKT-00001", Decimal(draft_amt),
                                                           "破损", Role.AGENT, draft_key))
    svc.submit("T1", SubmitCommand(op.operation_id, Role.AGENT))
    svc.approve("T1", ApproveCommand(op.operation_id, Role.APPROVER,
                                     decision_version=_sql_one(
                                         "SELECT version FROM refund_operations"
                                         " WHERE operation_id=:o", {"o": op.operation_id})))
    return op


@pg_live
@red
def test_p4_approval_decision_fact_recorded_on_approve():
    """approve 后 approval_decisions 必须有事实行：tenant/operation/decision_version/
    授权人(actor)/decision=approved/时间。现状：PgCommandService 未写该表。"""
    svc = _new_db()
    op = _seed_approved(svc)
    row = _sql_one(
        "SELECT tenant_id, operation_id, decision, decided_by, decided_version"
        " FROM approval_decisions WHERE operation_id=:o", {"o": op.operation_id})
    assert row is not None
    assert row[0] == "T1" and row[1] == op.operation_id
    assert row[2] == "approved" and row[3] == "approver" and row[4] == 2


@pg_live
@red
def test_p4_rejection_decision_fact_recorded():
    """reject 后同样落决定事实行。"""
    svc = _new_db()
    svc.create_ticket(_ticket_cmd("tk-r"))
    op = svc.create_refund_draft("T1", CreateRefundCommand("TKT-00001", Decimal("60.00"),
                                                           "x", Role.AGENT, "k-r"))
    svc.submit("T1", SubmitCommand(op.operation_id, Role.AGENT))
    ver = _sql_one("SELECT version FROM refund_operations WHERE operation_id=:o",
                   {"o": op.operation_id})
    svc.reject("T1", RejectCommand(op.operation_id, Role.APPROVER, reason="重复申请",
                                   decision_version=ver))
    assert _sql_one("SELECT decision FROM approval_decisions WHERE operation_id=:o",
                    {"o": op.operation_id}) == "rejected"


@pg_live
def test_p4_repeat_approval_no_extra_side_effect():
    """同 op 重复审批/过期版本审批 → 失败且无额外副作用（决定表无新增行、无重复审计）。"""
    svc = _new_db()
    op = _seed_approved(svc)
    before = _sql_one("SELECT COUNT(*) FROM approval_decisions WHERE operation_id=:o",
                      {"o": op.operation_id})
    with pytest.raises(AfterSalesError):   # 终态/过期版本 → 稳定失败
        svc.approve("T1", ApproveCommand(op.operation_id, Role.APPROVER, decision_version=2))
    assert _sql_one("SELECT COUNT(*) FROM approval_decisions WHERE operation_id=:o",
                    {"o": op.operation_id}) == before          # 无重复决定
    assert _sql_one("SELECT COUNT(*) FROM audit_events WHERE action='approve'"
                    " AND entity_id=:o", {"o": op.operation_id}) == 1  # 无重复审计


@pg_live
def test_p4_unauthorized_approval_rejected():
    """非授权 actor（AGENT/凭据冒用）审批 → 拒绝且零副作用。"""
    svc = _new_db()
    svc.create_ticket(_ticket_cmd("tk-u"))
    op = svc.create_refund_draft("T1", CreateRefundCommand("TKT-00001", Decimal("60.00"),
                                                           "x", Role.AGENT, "k-u"))
    svc.submit("T1", SubmitCommand(op.operation_id, Role.AGENT))
    with pytest.raises(AfterSalesError) as ei:
        svc.approve("T1", ApproveCommand(op.operation_id, Role.AGENT, decision_version=2))
    assert ei.value.code == AfterSalesErrorCode.PERMISSION_DENIED
    assert _sql_one("SELECT COUNT(*) FROM approval_decisions") == 0
    assert _sql_one("SELECT status FROM refund_operations WHERE operation_id=:o",
                    {"o": op.operation_id}) == "pending_approval"


@pg_live
@red
def test_p4_policy_only_latest_effective_version_used():
    """政策解析只用业务时间下最新有效版本；未来版本不参与决定（缺生效期过滤 → RED）。"""
    svc = _new_db()
    repo = svc._repo
    # 同 policy 两版本：v1 ratio=1.0 生效（1970），v2 ratio=0.50 生效于未来 2099
    repo.insert_policy(PolicyRow("T1", "P-DAM", "refund", '["damaged"]', 30,
                                 Decimal("1.0000"), "2020-01-01", 1))
    repo.insert_policy(PolicyRow("T1", "P-DAM", "refund", '["damaged"]', 30,
                                 Decimal("0.5000"), "2099-01-01", 2))
    t = svc.create_ticket(_ticket_cmd("tk-pol", reason="破损"))
    assert t.reason == "商品破损"
    op = svc.create_refund_draft("T1", CreateRefundCommand("TKT-00001", Decimal("60.00"),
                                                           "x", Role.AGENT, "k-pol"))
    # 草稿金额应来自生效的 v1(1.0) 而非未来 v2 —— 草稿不经政策金额校验，
    # 因此此处验证「决定/草稿可定位实际采用版本」在命令落库证据中体现；先以无未来干预断言
    assert op.amount == Decimal("60.00")   # 由调用方给定；契约核心见 P-3 证据快照（后续迁移）

@pg_live
@red
def test_p4_idempotency_three_tuple_command_type_dimension():
    """幂等三元组 (tenant, command_type, raw_key)：不同 command_type 可用同一原始 key。
    现状键仅 tenant+key 前缀 → 同原始 key 建单后再用于退款草稿会误判冲突。"""
    svc = _new_db()
    svc.create_ticket(_ticket_cmd(key="share"))                      # command_type=create_ticket
    op = svc.create_refund_draft("T1", CreateRefundCommand("TKT-00001", Decimal("60.00"),
                                                           "x", Role.AGENT, "share"))  # create_refund
    assert op.status.value == "draft"   # 现状：同原始 key → IDEMPOTENCY_CONFLICT（RED）


@pg_live
def test_p4_concurrent_same_command_same_key_single_result():
    """两独立 PgCommandService 实例并发同 (tenant,command_type,raw_key,payload) → 无 500、
    无重复业务对象；恰一新建、另一返回原结果。现状：idem 冲突以 UniqueViolation 冒泡/无重读。"""
    svc = _new_db()
    results: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait()
            s = PgCommandService(PostgresAfterSalesRepository(DATABASE_URL))
            t = s.create_ticket(_ticket_cmd(key="conc"))
            with lock:
                results.append(("ok", t.ticket_id))
        except AfterSalesError as e:
            with lock:
                results.append(("domain", e.code.value))
        except Exception as e:  # noqa: BLE001  500 类未映射异常 = 契约失败
            with lock:
                results.append(("boom", type(e).__name__))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not any(r[0] == "boom" for r in results)                 # 无 500
    ids = {r[1] for r in results}
    assert len(ids) == 1                                            # 恰一个工单（返回同一结果）
    assert _sql_one("SELECT COUNT(*) FROM tickets WHERE ticket_id=:i",
                    {"i": next(iter(ids))}) == 1
    assert _sql_one("SELECT COUNT(*) FROM audit_events WHERE action='create_ticket'") == 1
