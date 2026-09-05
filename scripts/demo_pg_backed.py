"""演示：PG-backed 领域会话（阶段二）。

业务事实（订单/工单/操作/审批流水/幂等/审计）落 PostgreSQL 行表；save→load 装载重建
（模拟进程重启）后：同幂等键重复请求返回原结果（不重复副作用）、可继续关单且审计追加。

前置：本地 PostgreSQL（见 docs/POSTGRES.md），表结构由 alembic/schema.sql 建立。
运行：.venv\\Scripts\\python.exe scripts/demo_pg_backed.py
"""
import os
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.domain.after_sales import (
    ApproveCommand,
    CloseTicketCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    PolicyRule,
    RequestType,
    Role,
    SubmitCommand,
)
from src.persistence.pg_backed import PgBackedSession
from src.repo import PostgresAfterSalesRepository

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot",
)
POL = PolicyRule(policy_id="P-DAMAGED-FULL", tenant_id="T1",
                 request_type=RequestType.REFUND, reason_tags=("damaged",),
                 window_days=30, refund_ratio=Decimal("1.00"))


def main() -> None:
    from tests.unit.domain.after_sales.helpers import baseline_service

    session = PgBackedSession(PostgresAfterSalesRepository(DATABASE_URL))

    print("== 1) 领域服务执行业务流程（订单 ORD-1 实付 100，破损退款 60）==")
    svc = baseline_service()
    t = svc.create_ticket(CreateTicketCommand("T1", "ORD-1", "C1", RequestType.REFUND,
                                              "商品破损", ("damaged",), Role.AGENT, "tk-1"))
    op = svc.create_refund(CreateRefundCommand(t.ticket_id, Decimal("60.00"),
                                               "破损", Role.AGENT, "k-1"))
    svc.submit(SubmitCommand(op.operation_id, Role.AGENT))
    op = svc.approve(ApproveCommand(op.operation_id, Role.APPROVER, 1))
    op = svc.execute(ExecuteCommand(op.operation_id, Role.SYSTEM, "success"))
    print(f"   created ticket={t.ticket_id} operation={op.operation_id} "
          f"status={op.status.value} refunded={svc.refunded_amount('ORD-1')}")

    print("== 2) save：业务事实整库镜像写入 PostgreSQL（单事务）==")
    session.save(svc)
    with create_engine(DATABASE_URL).connect() as conn:
        n_audit = conn.execute(text("SELECT COUNT(*) FROM audit_events")).scalar()
        n_op = conn.execute(text("SELECT COUNT(*) FROM refund_operations")).scalar()
        n_ap = conn.execute(text("SELECT COUNT(*) FROM approval_decisions")).scalar()
        n_idem = conn.execute(text("SELECT COUNT(*) FROM idempotency_records")).scalar()
    print(f"   PG 行表：operations={n_op} approvals={n_ap} idem={n_idem} audit={n_audit}")

    print("== 3) load：模拟进程重启，从 PostgreSQL 重建领域服务 ==")
    svc2 = session.load(policies=[POL])
    print(f"   loaded tickets={len(svc2.export_state()['tickets'])} "
          f"refunded={svc2.refunded_amount('ORD-1')} "
          f"audit={len(svc2.export_state()['audit'])}")

    print("== 4) 重启后同幂等键重复请求：返回原结果（不重复副作用）==")
    dup = svc2.create_ticket(CreateTicketCommand("T1", "ORD-1", "C1", RequestType.REFUND,
                                                 "商品破损", ("damaged",), Role.AGENT, "tk-1"))
    assert dup.ticket_id == t.ticket_id
    assert len(svc2.export_state()["tickets"]) == 1
    assert svc2.refunded_amount("ORD-1") == Decimal("60.00")
    print(f"   duplicate -> 原工单 {dup.ticket_id}，无新增工单/退款")

    print("== 5) 重启后继续关单：审计在重建实例上追加 ==")
    before = len(svc2.export_state()["audit"])
    svc2.close_ticket(CloseTicketCommand(t.ticket_id, Role.AGENT))
    print(f"   closed ticket={t.ticket_id} audit {before} -> "
          f"{len(svc2.export_state()['audit'])}")
    session.save(svc2)
    print("   save 完成（关单后状态已镜像回 PG）")


if __name__ == "__main__":
    main()
