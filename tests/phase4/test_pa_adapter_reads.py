"""PA2：AfterSalesApplicationPort tenant-first 只读面 —— 双后端（Memory/PgCommand）行为一致。"""
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    CreateTicketCommand,
    RequestType,
    Role,
)
from src.domain.after_sales.adapters import MemoryAdapter, PgCommandAdapter
from src.domain.after_sales.pg_commands import PgCommandService
from src.repo import MemoryAfterSalesRepository, OrderRow, PolicyRow
from tests.unit.domain.after_sales.helpers import service_with_policies


def _memory_port():
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))
    return MemoryAdapter(svc)


def _pg_port():
    repo = MemoryAfterSalesRepository()
    repo.insert_order(OrderRow("T1", "ORD-1", "C1", "delivered", Decimal("100.00"), 2))
    repo.insert_policy(PolicyRow("T1", "P-DAMAGED-FULL", "refund", '["damaged"]', 30,
                                 Decimal("1.0000"), "2020-01-01", 1))
    return PgCommandAdapter(PgCommandService(repo), repo)


@pytest.mark.parametrize("make_port", [_memory_port, _pg_port], ids=["memory", "pg"])
def test_reads_tenant_first_and_consistent(make_port):
    port = make_port()
    t = port.create_ticket(CreateTicketCommand(
        "T1", "ORD-1", "C1", RequestType.REFUND, "商品破损", ("damaged",), Role.AGENT, "tk-read"))
    # get_ticket/get_operation tenant-first
    assert port.get_ticket("T1", t.ticket_id).ticket_id == t.ticket_id
    with pytest.raises(Exception):
        port.get_ticket("T2", t.ticket_id)
    # get_order tenant-first
    order = port.get_order("T1", "ORD-1")
    assert order.paid_amount == Decimal("100.00")
    with pytest.raises(Exception):
        port.get_order("T2", "ORD-1")
    # 客户历史与退款计划
    assert [x.ticket_id for x in port.list_customer_tickets("T1", "C1")] == [t.ticket_id]
    assert port.list_customer_tickets("T1", "C2") == []
    plan = port.compute_refund_plan("T1", "ORD-1", RequestType.REFUND, ("damaged",))
    assert plan.amount == Decimal("100.00") and plan.policy_id == "P-DAMAGED-FULL"
    # 审计读取（两端非空，含 create_ticket）
    assert any(getattr(e, "action", None) == "create_ticket" for e in port.audit_log("T1"))
