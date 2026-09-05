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
