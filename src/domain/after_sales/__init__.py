"""电商售后领域插件（阶段 1）。

纯确定性领域包：无 LLM、无网络、无文件副作用。
角色与金额规约复用上级包 src/domain/models.py（只读，不改动）。
"""
from ..models import Role
from .models import (
    AfterSalesError,
    AfterSalesErrorCode,
    AfterSalesTicket,
    ApproveCommand,
    AuditEvent,
    CloseTicketCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    Operation,
    OperationStatus,
    OperationType,
    Order,
    OrderItem,
    OrderStatus,
    ReconcileCommand,
    RejectCommand,
    RefundPlan,
    RequestType,
    SubmitCommand,
    TicketStatus,
)
from .policies import PolicyRule, detect_conflict, match_policies
from .service import AfterSalesService

__all__ = [
    "AfterSalesError",
    "AfterSalesErrorCode",
    "AfterSalesService",
    "AfterSalesTicket",
    "ApproveCommand",
    "AuditEvent",
    "CloseTicketCommand",
    "CreateRefundCommand",
    "CreateTicketCommand",
    "ExecuteCommand",
    "Operation",
    "OperationStatus",
    "OperationType",
    "Order",
    "OrderItem",
    "OrderStatus",
    "PolicyRule",
    "ReconcileCommand",
    "RejectCommand",
    "RefundPlan",
    "RequestType",
    "Role",
    "SubmitCommand",
    "TicketStatus",
    "detect_conflict",
    "match_policies",
]
