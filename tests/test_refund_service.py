"""退款领域服务单元测试：覆盖正常路径与失败路径。

运行：在项目根目录执行 `python -m pytest tests/ -v`
"""
from decimal import Decimal

import pytest

from src.domain.models import (
    ApproveCommand,
    CreateRefundCommand,
    DomainError,
    ErrorCode,
    ExecuteCommand,
    RefundStatus,
    RejectCommand,
    Role,
    SubmitCommand,
)
from src.domain.refund_service import RefundService


def _service(paid: str = "100.00") -> RefundService:
    svc = RefundService()
    svc.seed_order("ORD-1", paid)
    return svc


def _draft(svc, amount="50.00", key="key-1", ticket="T-1", actor=Role.AGENT, reason="商品破损"):
    return svc.create_draft(CreateRefundCommand(
        ticket_id=ticket,
        order_id="ORD-1",
        amount=Decimal(amount),
        reason=reason,
        actor=actor,
        idempotency_key=key,
    ))


# ---------- 正常路径 ----------

def test_full_happy_path():
    svc = _service()
    draft = _draft(svc)
    assert draft.status == RefundStatus.DRAFT

    draft = svc.submit_for_approval(SubmitCommand(draft.refund_id, Role.AGENT))
    assert draft.status == RefundStatus.PENDING_APPROVAL

    draft = svc.approve(ApproveCommand(draft.refund_id, Role.APPROVER, decision_version=1))
    assert draft.status == RefundStatus.APPROVED
    assert draft.decision_version == 1

    draft = svc.execute(ExecuteCommand(draft.refund_id, Role.SYSTEM))
    assert draft.status == RefundStatus.EXECUTED
    assert draft.executed is True

    # 审计：create / submit / approve / execute 共 4 条
    assert [e.action for e in svc.audit_log()] == [
        "create_draft", "submit_for_approval", "approve", "execute",
    ]


def test_idempotent_same_payload_returns_same_refund():
    svc = _service()
    first = _draft(svc, key="dup-1")
    second = _draft(svc, key="dup-1")
    assert first.refund_id == second.refund_id
    # 幂等命中，未重复创建草稿
    assert len(svc.audit_log()) == 1


# ---------- 失败路径：金额 ----------

def test_rejects_non_positive_amount():
    svc = _service()
    for bad in ("0.00", "-5.00"):
        with pytest.raises(DomainError) as e:
            _draft(svc, amount=bad, key=f"k-{bad}")
        assert e.value.code == ErrorCode.AMOUNT_NOT_POSITIVE


def test_rejects_amount_exceeding_paid():
    svc = _service(paid="100.00")
    with pytest.raises(DomainError) as e:
        _draft(svc, amount="100.01")
    assert e.value.code == ErrorCode.AMOUNT_EXCEEDS_PAID


def test_rejects_cumulative_exceeding_paid():
    svc = _service(paid="100.00")
    first = _draft(svc, amount="60.00", key="a")
    svc.submit_for_approval(SubmitCommand(first.refund_id, Role.AGENT))
    svc.approve(ApproveCommand(first.refund_id, Role.APPROVER, decision_version=1))
    svc.execute(ExecuteCommand(first.refund_id, Role.SYSTEM))

    with pytest.raises(DomainError) as e:
        _draft(svc, amount="50.00", key="b")
    assert e.value.code == ErrorCode.AMOUNT_EXCEEDS_REMAINING


def test_rejects_float_amount():
    svc = _service()
    with pytest.raises(ValueError):
        svc.create_draft(CreateRefundCommand(
            ticket_id="T-1", order_id="ORD-1", amount=50.5,  # 禁止 float
            reason="商品破损", actor=Role.AGENT, idempotency_key="f-1",
        ))


# ---------- 失败路径：权限 ----------

def test_rejects_non_agent_creating_draft():
    svc = _service()
    with pytest.raises(DomainError) as e:
        _draft(svc, actor=Role.CUSTOMER)
    assert e.value.code == ErrorCode.PERMISSION_DENIED


def test_rejects_agent_approving():
    svc = _service()
    draft = _draft(svc)
    svc.submit_for_approval(SubmitCommand(draft.refund_id, Role.AGENT))
    with pytest.raises(DomainError) as e:
        svc.approve(ApproveCommand(draft.refund_id, Role.AGENT, decision_version=1))
    assert e.value.code == ErrorCode.PERMISSION_DENIED


def test_rejects_approver_executing():
    svc = _service()
    draft = _draft(svc)
    svc.submit_for_approval(SubmitCommand(draft.refund_id, Role.AGENT))
    svc.approve(ApproveCommand(draft.refund_id, Role.APPROVER, decision_version=1))
    with pytest.raises(DomainError) as e:
        svc.execute(ExecuteCommand(draft.refund_id, Role.APPROVER))
    assert e.value.code == ErrorCode.PERMISSION_DENIED


# ---------- 失败路径：状态机 ----------

def test_rejects_execute_before_approval():
    svc = _service()
    draft = _draft(svc)  # DRAFT
    with pytest.raises(DomainError) as e:
        svc.execute(ExecuteCommand(draft.refund_id, Role.SYSTEM))
    assert e.value.code == ErrorCode.INVALID_STATE_TRANSITION


def test_rejects_execute_after_reject():
    svc = _service()
    draft = _draft(svc)
    svc.submit_for_approval(SubmitCommand(draft.refund_id, Role.AGENT))
    svc.reject(RejectCommand(draft.refund_id, Role.APPROVER, reason="不符合政策"))
    with pytest.raises(DomainError) as e:
        svc.execute(ExecuteCommand(draft.refund_id, Role.SYSTEM))
    assert e.value.code == ErrorCode.INVALID_STATE_TRANSITION


def test_rejects_approve_without_submit():
    svc = _service()
    draft = _draft(svc)  # DRAFT，未提交
    with pytest.raises(DomainError) as e:
        svc.approve(ApproveCommand(draft.refund_id, Role.APPROVER, decision_version=1))
    assert e.value.code == ErrorCode.INVALID_STATE_TRANSITION


# ---------- 失败路径：版本 / 幂等 ----------

def test_rejects_stale_decision_version():
    svc = _service()
    draft = _draft(svc)
    svc.submit_for_approval(SubmitCommand(draft.refund_id, Role.AGENT))
    with pytest.raises(DomainError) as e:
        svc.approve(ApproveCommand(draft.refund_id, Role.APPROVER, decision_version=2))
    assert e.value.code == ErrorCode.DECISION_VERSION_MISMATCH


def test_rejects_idempotency_conflict_on_different_payload():
    svc = _service()
    _draft(svc, key="same-key", amount="10.00")
    with pytest.raises(DomainError) as e:
        _draft(svc, key="same-key", amount="20.00")
    assert e.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
