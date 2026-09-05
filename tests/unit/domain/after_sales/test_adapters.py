"""AfterSalesApplicationPort 双后端 Adapter 等价测试（memory 与 pg-first 后端，同一场景）。"""
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesError,
    ApproveCommand,
    CloseTicketCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    RequestType,
    Role,
    SubmitCommand,
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


@pytest.mark.parametrize("make_port", [_memory_port, _pg_port],
                         ids=["memory", "pg"])
def test_adapter_full_flow_equivalent(make_port):
    """两端同一 port 场景：建单→草稿→提交→审批(decided_by)→执行→查询/关单。"""
    port = make_port()
    t = port.create_ticket(CreateTicketCommand(
        "T1", "ORD-1", "C1", RequestType.REFUND, "商品破损", ("damaged",), Role.AGENT, "tk-a"))
    assert t.ticket_id is not None
    op = port.create_refund_draft("T1", CreateRefundCommand(t.ticket_id, Decimal("60.00"),
                                                            "破损", Role.AGENT, "k-a"))
    assert op.amount == Decimal("60.00")
    # 同键同载荷幂等 → 原草稿
    again = port.create_refund_draft("T1", CreateRefundCommand(t.ticket_id, Decimal("60.00"),
                                                               "破损", Role.AGENT, "k-a"))
    assert again.operation_id == op.operation_id
    port.submit("T1", SubmitCommand(op.operation_id, Role.AGENT))
    ver = port.get_operation("T1", op.operation_id).version
    approved = port.approve("T1", ApproveCommand(op.operation_id, Role.APPROVER,
                                                 decision_version=ver),
                            decided_by="approver-1")
    assert approved.status.value == "approved"
    executed = port.execute("T1", ExecuteCommand(op.operation_id, Role.SYSTEM, "success"))
    assert executed.status.value == "executed"
    assert port.get_operation("T1", op.operation_id).status.value == "executed"
    closed = port.close_ticket("T1", CloseTicketCommand(t.ticket_id, Role.AGENT))
    assert closed.status.value == "closed"


@pytest.mark.parametrize("make_port", [_memory_port, _pg_port],
                         ids=["memory", "pg"])
def test_adapter_cross_tenant_read_rejected(make_port):
    """跨租户读被拒（两实现同为安全拒绝）。"""
    port = make_port()
    t = port.create_ticket(CreateTicketCommand(
        "T1", "ORD-1", "C1", RequestType.REFUND, "商品破损", ("damaged",), Role.AGENT, "tk-x"))
    with pytest.raises(AfterSalesError):
        port.get_ticket("T2", t.ticket_id)


@pytest.mark.parametrize("make_port", [_memory_port, _pg_port],
                         ids=["memory", "pg"])
def test_adapter_unauthorized_approve_rejected(make_port):
    """Agent 冒用审批 → PERMISSION_DENIED（两端一致）。"""
    port = make_port()
    t = port.create_ticket(CreateTicketCommand(
        "T1", "ORD-1", "C1", RequestType.REFUND, "商品破损", ("damaged",), Role.AGENT, "tk-u"))
    op = port.create_refund_draft("T1", CreateRefundCommand(t.ticket_id, Decimal("60.00"),
                                                            "x", Role.AGENT, "k-u"))
    port.submit("T1", SubmitCommand(op.operation_id, Role.AGENT))
    ver = port.get_operation("T1", op.operation_id).version
    with pytest.raises(AfterSalesError) as ei:
        port.approve("T1", ApproveCommand(op.operation_id, Role.AGENT, decision_version=ver),
                     decided_by="agent-1")
    assert ei.value.code.value.startswith("AFTER_SALES_PERMISSION_DENIED")
