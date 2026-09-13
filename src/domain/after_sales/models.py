"""电商售后领域插件 —— 纯数据模型、枚举、错误与强类型命令。

确定性约束（对齐 AGENTS.md 宪法第三、四、六条）：
- 本模块不含任何 LLM / 自然语言逻辑；金额用 Decimal，禁止 float。
- 角色语义复用 `src/domain/models.py` 的 Role；金额规约复用其 parse_money。
- 错误码独立命名（AFTER_SALES 语义），不与共享规约之外的任何历史错误码混用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple

from ..models import Role, parse_money  # noqa: F401  只读复用上级包


# ---------- 错误 ----------

class AfterSalesErrorCode(str, Enum):
    """售后插件领域错误码。领域服务抛出的错误不得被上层改写成成功。"""

    # 缺参与参数
    MISSING_REQUIRED_FIELD = "AFTER_SALES_MISSING_REQUIRED_FIELD"
    # 订单核验
    ORDER_NOT_FOUND = "AFTER_SALES_ORDER_NOT_FOUND"
    TENANT_MISMATCH = "AFTER_SALES_TENANT_MISMATCH"
    ORDER_STATUS_NOT_ELIGIBLE = "AFTER_SALES_ORDER_STATUS_NOT_ELIGIBLE"
    # 政策证据
    POLICY_NOT_FOUND = "AFTER_SALES_POLICY_NOT_FOUND"        # 无适用政策 → 证据不足，转人工
    POLICY_CONFLICT = "AFTER_SALES_POLICY_CONFLICT"          # 冲突政策 → 转人工
    # 实体
    TICKET_NOT_FOUND = "AFTER_SALES_TICKET_NOT_FOUND"
    OPERATION_NOT_FOUND = "AFTER_SALES_OPERATION_NOT_FOUND"
    # 权限
    PERMISSION_DENIED = "AFTER_SALES_PERMISSION_DENIED"
    # 金额
    AMOUNT_NOT_POSITIVE = "AFTER_SALES_AMOUNT_NOT_POSITIVE"
    AMOUNT_EXCEEDS_REMAINING = "AFTER_SALES_AMOUNT_EXCEEDS_REMAINING"
    # 状态与版本
    INVALID_STATE_TRANSITION = "AFTER_SALES_INVALID_STATE_TRANSITION"
    DECISION_VERSION_MISMATCH = "AFTER_SALES_DECISION_VERSION_MISMATCH"
    TICKET_HAS_OPEN_OPERATIONS = "AFTER_SALES_TICKET_HAS_OPEN_OPERATIONS"
    # 幂等与未知状态
    IDEMPOTENCY_CONFLICT = "AFTER_SALES_IDEMPOTENCY_CONFLICT"
    OPERATION_UNKNOWN_CONFLICT = "AFTER_SALES_OPERATION_UNKNOWN_CONFLICT"


class AfterSalesError(Exception):
    """售后插件统一领域异常，携带结构化错误码。"""

    def __init__(self, code: AfterSalesErrorCode, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code.value}] {message}")


# ---------- 状态枚举 ----------

class OrderStatus(str, Enum):
    PAID = "paid"          # 已支付，可售后
    SHIPPED = "shipped"    # 已发货，可售后
    DELIVERED = "delivered"  # 已签收，可售后（政策窗口内）
    CLOSED = "closed"      # 已关闭，不可售后


class RequestType(str, Enum):
    """售后诉求类型（V1 聚焦退款；退货/换货为后续扩展）。"""
    REFUND = "refund"
    RETURN = "return"
    EXCHANGE = "exchange"


class TicketStatus(str, Enum):
    """工单状态机：OPEN 受理 → RESOLVED/REJECTED 定性 → CLOSED 终态。"""
    OPEN = "open"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    CLOSED = "closed"


TICKET_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.OPEN: {TicketStatus.RESOLVED, TicketStatus.REJECTED},
    TicketStatus.RESOLVED: {TicketStatus.CLOSED},
    TicketStatus.REJECTED: {TicketStatus.CLOSED},
    TicketStatus.CLOSED: set(),
}


class OperationType(str, Enum):
    """操作类型。V1 实现 REFUND；其余为预留，不承诺能力。"""
    REFUND = "refund"
    CHANGE_ADDRESS = "change_address"  # 预留
    CLOSE_TICKET = "close_ticket"      # 预留


class OperationStatus(str, Enum):
    """操作状态机（动作草稿 → 审批 → 执行 / 未知恢复）。"""
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"      # 终态
    EXECUTED = "executed"      # 终态
    UNKNOWN = "unknown"        # 外部结果不明，只能对账收口，非终态
    FAILED = "failed"          # 终态（对账确认失败）


OPERATION_TRANSITIONS: dict[OperationStatus, set[OperationStatus]] = {
    OperationStatus.DRAFT: {OperationStatus.PENDING_APPROVAL, OperationStatus.REJECTED},
    OperationStatus.PENDING_APPROVAL: {OperationStatus.APPROVED, OperationStatus.REJECTED},
    OperationStatus.APPROVED: {OperationStatus.EXECUTED, OperationStatus.UNKNOWN},
    OperationStatus.UNKNOWN: {OperationStatus.EXECUTED, OperationStatus.FAILED},  # 仅允许原键对账
    OperationStatus.REJECTED: set(),
    OperationStatus.EXECUTED: set(),
    OperationStatus.FAILED: set(),
}

TERMINAL_OPERATION_STATUSES = frozenset({
    OperationStatus.REJECTED, OperationStatus.EXECUTED, OperationStatus.FAILED,
})


# ---------- 实体 ----------

@dataclass
class OrderItem:
    """订单明细行（V1 用于展示与商品证据，退款上限以 Order.paid_amount 为准）。"""
    sku: str
    name: str
    quantity: int
    unit_price: Decimal = Decimal("0.00")


@dataclass
class Order:
    order_id: str
    tenant_id: str
    customer_id: str
    status: OrderStatus
    paid_amount: Decimal
    items: list[OrderItem] = field(default_factory=list)
    days_since_sign: int = 0  # 签收后天数（V1 用整型简化，供政策窗口判定）

    def __post_init__(self) -> None:
        self.paid_amount = parse_money(self.paid_amount)


@dataclass(frozen=True)
class RefundPlan:
    """确定性退款计划（Agent 仅搬运此结果用于展示/草稿参数，不得自行决定金额）。

    amount = 实付 × 适用政策 refund_ratio（分精度）；由领域服务 compute_refund_plan 产出。
    """
    amount: Decimal
    refund_ratio: Decimal
    policy_id: str
    order_id: str


@dataclass
class AfterSalesTicket:
    """售后工单实体：承载工单生命周期状态与诉求证据。"""
    ticket_id: str
    tenant_id: str
    order_id: str
    customer_id: str
    request_type: RequestType
    reason: str
    reason_tags: Tuple[str, ...]
    status: TicketStatus
    created_by: Role
    resolution: Optional[str] = None
    version: int = 1


@dataclass
class Operation:
    """受控操作实体：草稿 → 审批 → 执行 / 未知恢复，全程幂等键与版本约束。"""
    operation_id: str
    ticket_id: str
    tenant_id: str
    order_id: str
    op_type: OperationType
    amount: Optional[Decimal]
    status: OperationStatus
    idempotency_key: Optional[str]
    created_by: Role
    version: int = 1
    decision_version: Optional[int] = None
    executed: bool = False


@dataclass(frozen=True)
class AuditEvent:
    """不可变审计事件（追加式），覆盖工单与操作，独立于任何模型输出。"""
    action: str
    entity_type: str  # "ticket" | "operation"
    entity_id: str
    actor: Role
    before: Optional[str]
    after: Optional[str]
    idempotency_key: Optional[str] = None
    note: Optional[str] = None
    event_id: Optional[str] = None


# ---------- 强类型命令（对齐宪法第四条：写操作强类型参数） ----------

@dataclass(frozen=True)
class CreateTicketCommand:
    """录入售后工单。reason_tags 为结构化诉求标签（如 ("damaged",)），
    由上层（阶段 2 Agent / 规则层）从自然语言抽取，领域层不做 NLU。"""
    tenant_id: str
    order_id: str
    customer_id: str
    request_type: RequestType
    reason: str
    reason_tags: Tuple[str, ...]
    actor: Role
    idempotency_key: str


@dataclass(frozen=True)
class CreateRefundCommand:
    """创建退款动作草稿（Agent 可执行；amount 仅接受 str/Decimal，禁止 float）。"""
    ticket_id: str
    amount: Decimal
    reason_detail: str
    actor: Role
    idempotency_key: str


@dataclass(frozen=True)
class SubmitCommand:
    operation_id: str
    actor: Role


@dataclass(frozen=True)
class ApproveCommand:
    operation_id: str
    actor: Role
    decision_version: int


@dataclass(frozen=True)
class RejectCommand:
    operation_id: str
    actor: Role
    reason: str
    decision_version: int  # 期望的操作版本（并发控制：版本不符拒绝）


@dataclass(frozen=True)
class ExecuteCommand:
    """执行已批准操作。external_result:
    - "success"：外部确认成功；
    - "timeout"：外部调用超时 / 结果不明 → 进入 operation_unknown。"""
    operation_id: str
    actor: Role
    external_result: str = "success"


@dataclass(frozen=True)
class ReconcileCommand:
    """operation_unknown 对账收口：只能以原操作与结果成功/失败收口，禁止换键重试。"""
    operation_id: str
    actor: Role
    result: str  # "success" | "failed"


@dataclass(frozen=True)
class CloseTicketCommand:
    ticket_id: str
    actor: Role
