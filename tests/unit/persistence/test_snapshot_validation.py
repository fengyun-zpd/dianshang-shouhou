"""快照严格结构/语义校验（任务卡 J）：非法快照必须统一拒绝为 SnapshotCorruptionError。

覆盖：未知顶层字段、缺失必需字段、未知枚举、非法/非有限 Decimal（NaN/Infinity）、
负金额、引用关系错误、租户不一致、退款累计超实付、seq 非单调、状态组合非法。
"""
import pytest

from src.agents import WorkflowRunner
from src.persistence import RecoverableSession, SnapshotCorruptionError
from src.persistence.codec import state_to_jsonable
from tests.unit.domain.after_sales.helpers import service_with_policies

POL = (("P-DAMAGED-FULL", ("damaged",), "1.00", 30),)


def valid_snapshot() -> dict:
    svc = service_with_policies(*POL)
    WorkflowRunner(svc).start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="jv")
    return state_to_jsonable(svc.export_state())


def load_rejects(snapshot: dict, tmp_path) -> None:
    db = tmp_path / "jv.db"
    session = RecoverableSession(db)
    session.store.save_snapshot(snapshot)
    with pytest.raises(SnapshotCorruptionError):
        session.load()


def test_valid_snapshot_loads(tmp_path):
    db = tmp_path / "ok.db"
    session = RecoverableSession(db)
    session.store.save_snapshot(valid_snapshot())
    assert session.load() is not None


def test_unknown_top_level_field_rejected(tmp_path):
    snap = valid_snapshot()
    snap["hacked"] = {"@d": "1.00"}
    load_rejects(snap, tmp_path)


def test_missing_required_field_rejected(tmp_path):
    snap = valid_snapshot()
    del snap["policies"]
    load_rejects(snap, tmp_path)


def test_unknown_enum_value_rejected(tmp_path):
    snap = valid_snapshot()
    op = next(iter(snap["operations"].values()))
    op["status"] = {"@e": "OperationStatus", "v": "warped"}
    load_rejects(snap, tmp_path)


def test_refunded_exceeding_paid_rejected(tmp_path):
    snap = valid_snapshot()
    snap["refunded"]["ORD-1"] = {"@d": "99999.00"}
    load_rejects(snap, tmp_path)


def test_negative_amount_rejected(tmp_path):
    snap = valid_snapshot()
    snap["refunded"]["ORD-1"] = {"@d": "-5.00"}
    load_rejects(snap, tmp_path)


def test_infinity_and_nan_rejected(tmp_path):
    for bad in ("Infinity", "NaN"):
        snap = valid_snapshot()
        order = snap["orders"]["ORD-1"]
        order["paid_amount"] = {"@d": bad}
        load_rejects(snap, tmp_path)


def test_broken_ticket_order_reference_rejected(tmp_path):
    snap = valid_snapshot()
    ticket = next(iter(snap["tickets"].values()))
    ticket["order_id"] = "ORD-NOPE"          # 引用不存在订单
    load_rejects(snap, tmp_path)


def test_broken_operation_ticket_reference_rejected(tmp_path):
    snap = valid_snapshot()
    op = next(iter(snap["operations"].values()))
    op["ticket_id"] = "TKT-NOPE"
    load_rejects(snap, tmp_path)


def test_tenant_inconsistency_rejected(tmp_path):
    snap = valid_snapshot()
    ticket = next(iter(snap["tickets"].values()))
    ticket["tenant_id"] = "T2"               # 与其订单（T1）不一致
    load_rejects(snap, tmp_path)


def test_seq_regression_rejected(tmp_path):
    snap = valid_snapshot()
    snap["seq"] = 0                          # 已有 TKT-00001 / OP-00002
    load_rejects(snap, tmp_path)


def test_operation_status_executed_mismatch_rejected(tmp_path):
    snap = valid_snapshot()
    op = next(iter(snap["operations"].values()))
    op["executed"] = True                    # 状态仍非 EXECUTED
    load_rejects(snap, tmp_path)
