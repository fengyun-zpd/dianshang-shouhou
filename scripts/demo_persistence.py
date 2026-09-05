"""演示：SQLite 可恢复持久化（重启恢复唯一事实源原型）。

运行（项目根，D 盘 .venv）：
    .venv\\Scripts\\python.exe scripts\\demo_persistence.py

流程：建服务 → 工作流到审批挂起 → 落库 → “重启”（新会话从快照恢复）
→ 恢复后状态/审计一致 → 审批通过并执行 → 再次落库 → 再次恢复核对。
"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agents import WorkflowRunner
from src.domain.after_sales import ApproveCommand, CloseTicketCommand, ExecuteCommand, Role
from src.domain.after_sales.adapters import MemoryAdapter
from src.persistence import RecoverableSession
from tests.unit.domain.after_sales.helpers import service_with_policies


def make_service():
    return service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))


def main() -> None:
    from src.platform.runtime_paths import runtime_tmp_dir
    db_path = Path(tempfile.mkdtemp(prefix="dsh-persist-", dir=str(runtime_tmp_dir()))) / "journal.db"
    print("=" * 60)
    print("演示：SQLite 可恢复持久化（唯一事实源原型）")
    print("=" * 60)
    print(f"journal db: {db_path}")

    # 第 1 轮：走到审批挂起并落库
    session1 = RecoverableSession(db_path, build_service=make_service)
    svc1 = session1.load()
    runner = WorkflowRunner(MemoryAdapter(svc1))
    r = runner.start("T1", "订单 ORD-1 商品破损，要求退款", thread_id="persist-demo")
    assert r.waiting_approval
    ticket_id, op_id = r.state["ticket_id"], r.state["operation_id"]
    session1.persist(svc1)
    n1 = session1.store.journal_size()
    print(f"[1] 第 1 轮：工单 {ticket_id} 待审批，审计 {len(svc1.audit_log())} 条，已落库（journal={n1}）")
    session1.close()

    # “重启”：新会话从快照恢复
    session2 = RecoverableSession(db_path, build_service=make_service)
    svc2 = session2.load()
    op = svc2.get_operation(op_id)
    print(f"[2] 重启恢复：操作 {op.operation_id} 状态={op.status.value}，"
          f"审计 {len(svc2.audit_log())} 条（与第 1 轮一致={svc2.export_state()['seq'] == svc1.export_state()['seq']}）")

    # 恢复后续跑：审批 → 执行 → 关单
    svc2.approve(ApproveCommand(op.operation_id, Role.APPROVER, decision_version=op.version))
    svc2.execute(ExecuteCommand(op.operation_id, Role.SYSTEM))
    svc2.close_ticket(CloseTicketCommand(ticket_id, Role.AGENT))
    session2.persist(svc2)
    print(f"[3] 恢复后续跑完成：退款 {svc2.refunded_amount('ORD-1')} 元，工单 {svc2.get_ticket(ticket_id).status.value}")
    session2.close()

    # 再次恢复核对
    session3 = RecoverableSession(db_path, build_service=make_service)
    svc3 = session3.load()
    actions = [e.action for e in svc3.audit_log()]
    print(f"[4] 再次恢复：退款 {svc3.refunded_amount('ORD-1')} 元（期望 100.00）；"
          f"execute×{actions.count('execute')}、close_ticket×{actions.count('close_ticket')}")
    assert svc3.refunded_amount("ORD-1") == Decimal("100.00")
    assert actions.count("execute") == 1
    assert actions.count("close_ticket") == 1
    assert svc3.get_ticket(ticket_id).status.value == "closed"
    session3.close()
    print("\n演示完成：状态/审计跨会话保真，恢复后可持续执行（幂等与状态机不受影响）。")


if __name__ == "__main__":
    main()
