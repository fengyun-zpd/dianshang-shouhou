"""K3：快照与恢复安全的严格类型与结构校验（RED 先于实现）。"""
import threading
from decimal import Decimal

import pytest

from src.agents import WorkflowRunner
from src.domain.after_sales import (
    AfterSalesService,
    ApproveCommand,
    ExecuteCommand,
    Role,
)
from src.domain.after_sales.adapters import MemoryAdapter
from src.persistence import RecoverableSession, SnapshotCorruptionError
from src.persistence.codec import state_to_jsonable
from tests.unit.domain.after_sales.helpers import service_with_policies

POL = (("P-DAMAGED-FULL", ("damaged",), "1.00", 30),)


def _valid() -> dict:
    """合法快照（JSON 化形式）。"""
    svc = service_with_policies(*POL)
    WorkflowRunner(MemoryAdapter(svc)).start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="k3")
    return state_to_jsonable(svc.export_state())


def _load_rejects(snapshot, tmp_path) -> None:
    db = tmp_path / "k3.db"
    s = RecoverableSession(db)
    s.store.save_snapshot(snapshot)
    with pytest.raises(SnapshotCorruptionError):
        s.load()


# ---------- 顶层类型严格（拒绝小数/截断/类型错误） ----------

def test_seq_float_rejected_no_truncation(tmp_path):
    snap = _valid()
    snap["seq"] = 1.9          # 小数：不得截断为 1 后通过
    _load_rejects(snap, tmp_path)


def test_seq_string_rejected(tmp_path):
    snap = _valid()
    snap["seq"] = "5"
    _load_rejects(snap, tmp_path)


def test_schema_version_float_rejected(tmp_path):
    snap = _valid()
    snap["schema_version"] = 1.0  # float 类型（值=1 也不许，须 int 字面）
    _load_rejects(snap, tmp_path)


def test_orders_not_dict_rejected(tmp_path):
    snap = _valid()
    snap["orders"] = ["ORD-1"]
    _load_rejects(snap, tmp_path)


def test_policies_not_list_rejected(tmp_path):
    snap = _valid()
    snap["policies"] = {"P": "x"}
    _load_rejects(snap, tmp_path)


def test_tickets_not_dict_rejected(tmp_path):
    snap = _valid()
    snap["tickets"] = []
    _load_rejects(snap, tmp_path)


# ---------- 嵌套结构 ----------

def test_reason_tags_not_tuple_rejected(tmp_path):
    snap = _valid()
    ticket = next(iter(snap["tickets"].values()))
    ticket["reason_tags"] = ["damaged"]          # JSON 化应为 {"@t": [...]}
    _load_rejects(snap, tmp_path)


def test_operation_tenant_mismatch_rejected(tmp_path):
    snap = _valid()
    op = next(iter(snap["operations"].values()))
    op["tenant_id"] = "T2"                        # 与其订单/工单（T1）不一致
    _load_rejects(snap, tmp_path)


def test_refunded_value_not_decimal_rejected(tmp_path):
    snap = _valid()
    # 手工构造一个退款金额的非 Decimal 记录（作已执行累计形状）
    op = next(iter(snap["operations"].values()))
    op["status"] = {"@e": "OperationStatus", "v": "executed"}
    op["executed"] = True
    snap["refunded"] = {"ORD-1": "100.00"}        # 应为 {"@d": "100.00"}
    _load_rejects(snap, tmp_path)


# ---------- 恢复失败零部分变更 + 重复/并发恢复 ----------

def test_restore_bad_into_service_leaves_state_unchanged(tmp_path):
    svc = service_with_policies(*POL)
    WorkflowRunner(MemoryAdapter(svc)).start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="r1")
    before = svc.export_state()
    db = tmp_path / "k3b.db"
    s = RecoverableSession(db)
    bad = _valid()
    bad["tickets"] = []            # 类型/结构破坏（且引用断裂）
    with pytest.raises(SnapshotCorruptionError):
        s.restore_into(svc, bad)
    assert svc.export_state() == before


def test_direct_restore_state_bad_is_atomic(tmp_path):
    svc = service_with_policies(*POL)
    WorkflowRunner(MemoryAdapter(svc)).start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="r2")
    before = svc.export_state()
    bad = _valid()
    bad["tickets"] = []            # 原始 export 同形但 tickets 非法
    from src.persistence.codec import state_from_jsonable
    with pytest.raises((SnapshotCorruptionError, TypeError, ValueError)):
        svc.restore_state(state_from_jsonable(bad))
    assert svc.export_state() == before        # 任何失败不得部分改变


def test_repeated_restore_is_stable(tmp_path):
    db = tmp_path / "k3c.db"
    s1 = RecoverableSession(db)
    svc = service_with_policies(*POL)
    WorkflowRunner(MemoryAdapter(svc)).start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="r3")
    snap = s1.store.save_snapshot(state_to_jsonable(svc.export_state()))
    for _ in range(3):
        s2 = RecoverableSession(db)
        assert s2.load().export_state() == svc.export_state()
    assert s1.store.journal_size() == 1        # 重复读取不追加


def test_concurrent_restore_loads_are_consistent(tmp_path):
    db = tmp_path / "k3d.db"
    svc = service_with_policies(*POL)
    WorkflowRunner(MemoryAdapter(svc)).start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="r4")
    RecoverableSession(db).persist(svc)

    barrier = threading.Barrier(4)
    results: list[dict] = []
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait()
            s = RecoverableSession(db)
            results.append(s.load().export_state())
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(results) == 4
    for r in results[1:]:
        assert r == results[0]


# ---------- 跨租户伪造快照（换租户语义） ----------

def test_forged_cross_tenant_snapshot_rejected(tmp_path):
    snap = _valid()
    order = snap["orders"]["ORD-1"]
    order["tenant_id"] = "T9"                       # 整个订单换租户 → 其工单/操作租户全不一致
    _load_rejects(snap, tmp_path)


def test_valid_snapshot_after_execute_roundtrip(tmp_path):
    svc = service_with_policies(*POL)
    r = WorkflowRunner(MemoryAdapter(svc)).start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="r5")
    op = svc.get_operation(r.state["operation_id"])
    svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=op.version))
    svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    assert svc.refunded_amount("ORD-1") == Decimal("100.00")

    db = tmp_path / "k3e.db"
    RecoverableSession(db).persist(svc)
    restored = RecoverableSession(db).load()
    assert restored.export_state() == svc.export_state()
