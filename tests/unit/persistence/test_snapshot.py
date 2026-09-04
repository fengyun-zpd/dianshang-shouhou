"""SQLite 可恢复持久化原型测试：roundtrip 保真 / 审计 / 续跑 / 损坏拒绝 / 隔离。"""
import json
import sqlite3
from decimal import Decimal

import pytest

from src.agents import WorkflowRunner
from src.domain.after_sales import (
    AfterSalesService,
    ApproveCommand,
    CloseTicketCommand,
    CreateRefundCommand,
    ExecuteCommand,
    Role,
)
from src.persistence import RecoverableSession, SnapshotCorruptionError
from src.persistence.codec import state_from_jsonable, state_to_jsonable
from tests.unit.domain.after_sales.helpers import service_with_policies

REQUEST = "订单 ORD-1 商品破损，要求退款"


def make_service() -> AfterSalesService:
    return service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))


def drive_to_pending(svc: AfterSalesService) -> dict:
    """走到审批挂起，返回 op/ticket id。"""
    runner = WorkflowRunner(svc)
    r = runner.start("T1", REQUEST, thread_id="persist-t1")
    assert r.waiting_approval
    return {"ticket_id": r.state["ticket_id"], "operation_id": r.state["operation_id"],
            "thread": "persist-t1"}


def test_roundtrip_state_and_audit_fidelity(tmp_path):
    """落库→重建→恢复后状态与审计完全一致，并可继续审批执行。"""
    db = tmp_path / "a.db"

    svc1 = make_service()
    ids1 = drive_to_pending(svc1)
    s1 = RecoverableSession(db, build_service=make_service)
    s1.persist(svc1)
    s1.close()

    # 恢复
    s2 = RecoverableSession(db, build_service=make_service)
    svc2 = s2.load()
    assert svc2.export_state() == svc1.export_state()
    assert len(svc2.audit_log()) == len(svc1.audit_log())
    assert svc2.get_operation(ids1["operation_id"]).status.value == "pending_approval"

    # 恢复后可继续：审批 → 执行 → 关单
    op = svc2.get_operation(ids1["operation_id"])
    svc2.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=op.version))
    svc2.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    svc2.close_ticket(CloseTicketCommand(ids1["ticket_id"], Role.AGENT))
    assert svc2.refunded_amount("ORD-1") == Decimal("100.00")
    assert svc2.get_ticket(ids1["ticket_id"]).status.value == "closed"
    s2.persist(svc2)
    s2.close()

    # 再次恢复：执行结果与审计保留
    s3 = RecoverableSession(db, build_service=make_service)
    svc3 = s3.load()
    assert svc3.refunded_amount("ORD-1") == Decimal("100.00")
    actions = [e.action for e in svc3.audit_log()]
    assert actions.count("execute") == 1
    assert "close_ticket" in actions
    s3.close()


def test_idempotency_records_restored(tmp_path):
    db = tmp_path / "b.db"
    svc1 = make_service()
    ids = drive_to_pending(svc1)
    s1 = RecoverableSession(db, build_service=make_service)
    s1.persist(svc1)
    s1.close()

    s2 = RecoverableSession(db, build_service=make_service)
    svc2 = s2.load()
    ticket = svc2.get_ticket(ids["ticket_id"])
    # 用原工作流幂等键再次 create_refund：应返回原操作而非新建
    op = svc2.create_refund(CreateRefundCommand(
        ticket_id=ticket.ticket_id, amount=Decimal("100.00"),
        reason_detail="Agent 草稿：破损/售后退款（政策 P-DAMAGED-FULL）",
        actor=Role.AGENT, idempotency_key="wf:persist-t1:refund:" + ticket.ticket_id,
    ))
    assert op.operation_id == ids["operation_id"]
    s2.close()


def test_state_is_json_serializable(tmp_path):
    svc = make_service()
    drive_to_pending(svc)
    j = state_to_jsonable(svc.export_state())
    text = json.dumps(j, ensure_ascii=False, sort_keys=True)
    restored = state_from_jsonable(json.loads(text))
    assert restored["seq"] == svc.export_state()["seq"]
    assert "refunded" in restored


def test_corrupted_snapshot_rejected_fail_closed(tmp_path):
    db = tmp_path / "c.db"
    svc = make_service()
    s1 = RecoverableSession(db, build_service=make_service)
    s1.persist(svc)
    s1.close()

    # 篡改 journal 中的 payload
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE journal SET payload='{not-json' WHERE kind='snapshot'")
    conn.commit()
    conn.close()

    s2 = RecoverableSession(db, build_service=make_service)
    with pytest.raises(SnapshotCorruptionError):
        s2.load()  # fail-closed：损坏快照拒绝恢复，不产生半恢复服务
    # 明确校验走 latest_snapshot 也拒绝
    with pytest.raises(SnapshotCorruptionError):
        s2.store.latest_snapshot()


def test_checksum_mismatch_detected(tmp_path):
    db = tmp_path / "d.db"
    svc = make_service()
    s1 = RecoverableSession(db, build_service=make_service)
    s1.persist(svc)
    s1.close()
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT id, payload FROM journal WHERE kind='snapshot'").fetchone()
    payload = json.loads(row[1])
    payload["seq"] = 999  # 内容被改但 checksum 未更新
    conn.execute("UPDATE journal SET payload=? WHERE id=?",
                 (json.dumps(payload, ensure_ascii=False), row[0]))
    conn.commit()
    conn.close()
    with pytest.raises(SnapshotCorruptionError):
        RecoverableSession(db, build_service=make_service).load()


def test_cross_session_isolation(tmp_path):
    db1, db2 = tmp_path / "e1.db", tmp_path / "e2.db"
    svc1 = make_service()
    RecoverableSession(db1, build_service=make_service).persist(svc1)

    s2 = RecoverableSession(db2, build_service=make_service)
    svc2 = s2.load()
    assert len(svc2.audit_log()) == 0     # 独立库无历史
    assert s2.store.journal_size() == 0
    s2.close()
