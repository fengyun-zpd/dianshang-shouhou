"""原子恢复（任务卡 J）：先完整解析+校验，失败时原服务完全不变；通过后一次性替换。"""
from decimal import Decimal

import pytest

from src.agents import WorkflowRunner
from src.domain.after_sales import ApproveCommand, ExecuteCommand, Role
from src.persistence import RecoverableSession, SnapshotCorruptionError
from src.persistence.validate import validate_snapshot_state
from tests.unit.domain.after_sales.helpers import service_with_policies

POL = (("P-DAMAGED-FULL", ("damaged",), "1.00", 30),)


def _seed_pending() -> "object":
    svc = service_with_policies(*POL)
    r = WorkflowRunner(svc).start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="ar-p")
    return svc, r.state["operation_id"], r.state["ticket_id"]


def _seed_executed() -> "object":
    svc, op_id, _ticket_id = _seed_pending()
    op = svc.get_operation(op_id)
    svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=op.version))
    svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    return svc


def test_validate_is_separate_pure_step():
    """validate 独立于 restore 存在（先验后换）。"""
    svc, _op, _t = _seed_pending()
    assert validate_snapshot_state(svc.export_state()) is True


def test_validate_rejects_semantic_broken():
    svc = _seed_executed()
    state = svc.export_state()
    state["refunded"]["ORD-1"] = Decimal("999.00")   # 与已执行求和 100 不一致
    with pytest.raises(SnapshotCorruptionError):
        validate_snapshot_state(state)


def test_restore_failure_leaves_service_unchanged(tmp_path):
    svc = _seed_executed()
    before = svc.export_state()
    bad = svc.export_state().copy()
    bad["refunded"] = {"ORD-1": Decimal("999.00")}

    db = tmp_path / "ar.db"
    session = RecoverableSession(db)
    with pytest.raises(SnapshotCorruptionError):
        session.restore_into(svc, bad)               # bad 为已解码状态 → 直接校验失败
    # 原服务状态完全不变（原子：校验先于任何写入）
    assert svc.export_state() == before
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")


def test_restore_into_replaces_state_when_valid(tmp_path):
    svc = _seed_executed()
    other = _seed_pending()[0]

    db = tmp_path / "ar2.db"
    session = RecoverableSession(db)
    # 把 svc（已执行）恢复到 other（挂起中）的快照语义：直接经会话（含 JSON 编解码管线）
    from src.persistence.codec import state_to_jsonable
    session.persist(other)
    restored = RecoverableSession(db).load()
    session.restore_into(svc, state_to_jsonable(restored.export_state()))
    assert svc.export_state() == other.export_state()
    assert svc.refunded_amount("ORD-1") == Decimal("0.00")
