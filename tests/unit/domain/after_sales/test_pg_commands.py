"""PG-first 命令服务单测（MemoryAfterSalesRepository 后端；无 PG 依赖）。

覆盖：create_ticket（事务/幂等/政策/客户匹配/审计）、submit、approve/reject
（expected_version CAS、并发语义、事务回滚无部分提交）。真实 PostgreSQL 语义见
tests/integration/test_pg_commands_live.py。
"""
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    ApproveCommand,
    CreateTicketCommand,
    OperationStatus,
    RejectCommand,
    RequestType,
    Role,
    SubmitCommand,
)
from src.domain.after_sales.pg_commands import PgCommandService
from src.repo import (
    IdemRow,
    MemoryAfterSalesRepository,
    OperationRow,
    OrderRow,
    PolicyRow,
    TicketRow,
)


@pytest.fixture
def ctx():
    repo = MemoryAfterSalesRepository()
    repo.insert_order(OrderRow("T1", "ORD-1", "C1", "delivered", Decimal("100.00"), 2))
    repo.insert_policy(PolicyRow("T1", "P-1", "refund", '["damaged"]', 30,
                                 Decimal("1.0000"), "1970-01-01", 1))
    svc = PgCommandService(repo)
    return repo, svc


def _ticket_cmd(key="tk-1", tenant="T1", order="ORD-1", customer="C1", actor=Role.AGENT,
                reason="商品破损", tags=("damaged",)):
    return CreateTicketCommand(tenant, order, customer, RequestType.REFUND,
                               reason, tags, actor, key)


def _seed_draft(repo, op_id="OP-1", status="draft", key="k-1", amount="60.00"):
    repo.insert_ticket(TicketRow("T1", "TKT-1", "ORD-1", "C1", "refund", "破损", "open",
                                 created_by="agent", reason_tags='["damaged"]'))
    repo.insert_operation(OperationRow("T1", op_id, "TKT-1", "ORD-1", "refund",
                                       Decimal(amount), status, f"T1:{key}", "agent"))


# ---------- create_ticket ----------

def test_create_ticket_commits_ticket_idem_audit(ctx):
    repo, svc = ctx
    t = svc.create_ticket(_ticket_cmd())
    assert t.ticket_id == "TKT-00001"
    assert len(repo.list_tickets()) == 1
    assert repo.get_idem("T1", "T1:tk-1") is not None
    audits = [a for a in repo.list_audit() if a.entity_type == "ticket"]
    assert len(audits) == 1 and audits[0].action == "create_ticket"


def test_create_ticket_repeat_same_payload_idempotent(ctx):
    repo, svc = ctx
    t1 = svc.create_ticket(_ticket_cmd())
    t2 = svc.create_ticket(_ticket_cmd())
    assert t1.ticket_id == t2.ticket_id
    assert len(repo.list_tickets()) == 1


def test_create_ticket_same_key_different_payload_rejected(ctx):
    _, svc = ctx
    svc.create_ticket(_ticket_cmd())
    with pytest.raises(AfterSalesError) as ei:
        svc.create_ticket(_ticket_cmd(reason="少件", tags=("missing_item",)))
    assert ei.value.code == AfterSalesErrorCode.IDEMPOTENCY_CONFLICT


def test_create_ticket_customer_own_order_ok_and_foreign_rejected(ctx):
    _, svc = ctx
    svc.create_ticket(_ticket_cmd(actor=Role.CUSTOMER, customer="C1", key="tk-c1"))
    with pytest.raises(AfterSalesError) as ei:
        svc.create_ticket(_ticket_cmd(actor=Role.CUSTOMER, customer="C2", key="tk-c2"))
    assert ei.value.code == AfterSalesErrorCode.PERMISSION_DENIED


def test_create_ticket_no_policy_escalates():
    """无适用政策 → POLICY_NOT_FOUND（证据不足转人工）。"""
    repo = MemoryAfterSalesRepository()          # 无任何政策
    repo.insert_order(OrderRow("T1", "ORD-1", "C1", "delivered", Decimal("100.00"), 2))
    svc = PgCommandService(repo)
    with pytest.raises(AfterSalesError) as ei:
        svc.create_ticket(_ticket_cmd())
    assert ei.value.code == AfterSalesErrorCode.POLICY_NOT_FOUND


# ---------- submit / approve / reject ----------

def test_submit_pending_approval(ctx):
    repo, svc = ctx
    _seed_draft(repo)
    op = svc.submit("T1", SubmitCommand("OP-1", Role.AGENT))
    assert op.status == OperationStatus.PENDING_APPROVAL and op.version == 2
    assert any(a.action == "submit" for a in repo.list_audit())


def test_approve_cas_success_and_repeat_rejected(ctx):
    repo, svc = ctx
    _seed_draft(repo)
    svc.submit("T1", SubmitCommand("OP-1", Role.AGENT))
    op = svc.approve("T1", ApproveCommand("OP-1", Role.APPROVER, decision_version=2))
    assert op.status == OperationStatus.APPROVED
    assert op.decision_version == 2 and op.version == 3
    with pytest.raises(AfterSalesError) as ei:        # 过期 expected_version → CAS 拒绝
        svc.approve("T1", ApproveCommand("OP-1", Role.APPROVER, decision_version=2))
    assert ei.value.code == AfterSalesErrorCode.DECISION_VERSION_MISMATCH


def test_approve_rollback_no_partial_commit(ctx):
    """过期版本审批失败 → 状态/版本/审计零残留（整事务回滚）。"""
    repo, svc = ctx
    _seed_draft(repo)
    svc.submit("T1", SubmitCommand("OP-1", Role.AGENT))
    with pytest.raises(AfterSalesError):
        svc.approve("T1", ApproveCommand("OP-1", Role.APPROVER, decision_version=1))
    got = repo.get_operation("T1", "OP-1")
    assert got.status == "pending_approval" and got.version == 2   # 未迁移
    assert not any(a.action == "approve" for a in repo.list_audit())


def test_reject_path_and_role_gate(ctx):
    repo, svc = ctx
    _seed_draft(repo, op_id="OP-1")
    svc.submit("T1", SubmitCommand("OP-1", Role.AGENT))
    op = svc.reject("T1", RejectCommand("OP-1", Role.APPROVER, reason="重复申请",
                                        decision_version=2))
    assert op.status == OperationStatus.REJECTED and op.version == 3
    note = [a for a in repo.list_audit() if a.action == "reject"]
    assert note and note[0].note == "重复申请"
    with pytest.raises(AfterSalesError) as ei:        # 角色门禁
        svc.approve("T1", ApproveCommand("OP-1", Role.CUSTOMER, decision_version=9))
    assert ei.value.code == AfterSalesErrorCode.PERMISSION_DENIED


# ---------- 退款草稿 / 执行 / 对账 / 关单 ----------

from src.domain.after_sales import (  # noqa: E402
    CloseTicketCommand,
    CreateRefundCommand,
    ExecuteCommand,
    ReconcileCommand,
)


def _ticket(repo, status="open"):
    repo.insert_ticket(TicketRow("T1", "TKT-1", "ORD-1", "C1", "refund", "破损", status,
                                 created_by="agent", reason_tags='["damaged"]'))


def test_draft_idempotent_no_capacity_at_draft(ctx):
    """草稿创建幂等；草稿不占容量（容量在执行/对账成功期拦截）。"""
    repo, svc = ctx
    _ticket(repo)
    op = svc.create_refund_draft("T1", CreateRefundCommand("TKT-1", Decimal("60.00"),
                                                           "破损", Role.AGENT, "k-1"))
    assert op.status == OperationStatus.DRAFT and op.amount == Decimal("60.00")
    again = svc.create_refund_draft("T1", CreateRefundCommand("TKT-1", Decimal("60.00"),
                                                              "破损", Role.AGENT, "k-1"))
    assert again.operation_id == op.operation_id            # 同键幂等
    # 60+40+1 三张草稿均可建（未执行不占容量），累计仍 0
    svc.create_refund_draft("T1", CreateRefundCommand("TKT-1", Decimal("40.00"),
                                                      "破损", Role.AGENT, "k-2"))
    svc.create_refund_draft("T1", CreateRefundCommand("TKT-1", Decimal("1.00"),
                                                      "破损", Role.AGENT, "k-3"))
    assert len(repo.list_operations()) == 3
    assert repo.executed_sum_for_order("T1", "ORD-1") == Decimal("0.00")


def test_draft_unknown_order_guard(ctx):
    repo, svc = ctx
    _ticket(repo)
    repo.insert_operation(OperationRow("T1", "OP-UNKNOWN", "TKT-1", "ORD-1", "refund",
                                       Decimal("30.00"), "unknown", "T1:old", "agent"))
    with pytest.raises(AfterSalesError) as ei:
        svc.create_refund_draft("T1", CreateRefundCommand("TKT-1", Decimal("10.00"),
                                                          "x", Role.AGENT, "k-new"))
    assert ei.value.code == AfterSalesErrorCode.OPERATION_UNKNOWN_CONFLICT


def _submit_approve(svc, repo, op_id):
    svc.submit("T1", SubmitCommand(op_id, Role.AGENT))
    svc.approve("T1", ApproveCommand(op_id, Role.APPROVER,
                                     decision_version=repo.get_operation("T1", op_id).version))


def test_execute_capacity_and_timeout(ctx):
    repo, svc = ctx
    _ticket(repo)
    svc.create_refund_draft("T1", CreateRefundCommand("TKT-1", Decimal("60.00"),
                                                      "破损", Role.AGENT, "k-1"))
    svc.create_refund_draft("T1", CreateRefundCommand("TKT-1", Decimal("60.00"),
                                                      "再退", Role.AGENT, "k-2"))  # 草稿阶段按已执行=0 可建
    ops = sorted(repo.list_operations(), key=lambda r: r.operation_id)
    op1, op2 = ops[0].operation_id, ops[1].operation_id
    _submit_approve(svc, repo, op1)
    _submit_approve(svc, repo, op2)
    done = svc.execute("T1", ExecuteCommand(op1, Role.SYSTEM, external_result="success"))
    assert done.status == OperationStatus.EXECUTED
    assert repo.executed_sum_for_order("T1", "ORD-1") == Decimal("60.00")
    # 第二张已批准 60：执行被容量拒绝（不迁移不累计）
    with pytest.raises(AfterSalesError) as ei:
        svc.execute("T1", ExecuteCommand(op2, Role.SYSTEM, "success"))
    assert ei.value.code == AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING
    assert repo.executed_sum_for_order("T1", "ORD-1") == Decimal("60.00")
    # timeout → unknown（不累计）
    unk = svc.execute("T1", ExecuteCommand(op2, Role.SYSTEM, "timeout"))
    assert unk.status == OperationStatus.UNKNOWN
    assert repo.executed_sum_for_order("T1", "ORD-1") == Decimal("60.00")


def test_reconcile_original_only(ctx):
    """unknown 只能原 operation 对账：success → executed（容量）；终态后重复对账被拒。"""
    repo, svc = ctx
    _ticket(repo)
    svc.create_refund_draft("T1", CreateRefundCommand("TKT-1", Decimal("60.00"),
                                                      "破损", Role.AGENT, "k-1"))
    op_id = repo.list_operations()[0].operation_id
    _submit_approve(svc, repo, op_id)
    svc.execute("T1", ExecuteCommand(op_id, Role.SYSTEM, "timeout"))
    assert repo.get_operation("T1", op_id).status == "unknown"
    done = svc.reconcile("T1", ReconcileCommand(op_id, Role.SYSTEM, "success"))
    assert done.status == OperationStatus.EXECUTED
    assert repo.executed_sum_for_order("T1", "ORD-1") == Decimal("60.00")
    with pytest.raises(AfterSalesError) as ei:              # 终态重复对账 → 拒绝
        svc.reconcile("T1", ReconcileCommand(op_id, Role.SYSTEM, "success"))
    assert ei.value.code == AfterSalesErrorCode.INVALID_STATE_TRANSITION


def test_close_ticket_paths(ctx):
    repo, svc = ctx
    _ticket(repo)
    # 无未决操作 → 直接关单
    closed = svc.close_ticket("T1", CloseTicketCommand("TKT-1", Role.AGENT))
    assert closed.status.value == "closed"


def test_close_blocks_open_operations_and_qualifies(ctx):
    repo, svc = ctx
    _ticket(repo)
    svc.create_refund_draft("T1", CreateRefundCommand("TKT-1", Decimal("60.00"),
                                                      "破损", Role.AGENT, "k-1"))
    op_id = repo.list_operations()[0].operation_id
    with pytest.raises(AfterSalesError) as ei:               # 未决草稿 → 禁止关单
        svc.close_ticket("T1", CloseTicketCommand("TKT-1", Role.AGENT))
    assert ei.value.code == AfterSalesErrorCode.TICKET_HAS_OPEN_OPERATIONS
    svc.submit("T1", SubmitCommand(op_id, Role.AGENT))
    svc.approve("T1", ApproveCommand(op_id, Role.APPROVER,
                                     decision_version=repo.get_operation("T1", op_id).version))
    svc.execute("T1", ExecuteCommand(op_id, Role.SYSTEM, "success"))
    t = svc.close_ticket("T1", CloseTicketCommand("TKT-1", Role.AGENT))
    assert t.status.value == "closed" and t.resolution == "refunded"
