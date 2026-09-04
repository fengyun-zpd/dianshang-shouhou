"""确定性领域服务的强类型模型与错误定义。

本模块只包含纯数据、枚举与校验，不含任何 LLM 逻辑。
金额统一使用 Decimal，避免浮点误差导致的多退 / 少退。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional

CENT = Decimal("0.01")


def parse_money(value) -> Decimal:
    """将输入规约为精确到分的 Decimal。

    - 禁止 float：float 可能已含精度损失（如 0.1 + 0.2）。
    - 小数位超过两位直接报错，防止「多退 / 少退一分钱」。
    """
    if isinstance(value, float):
        raise ValueError("金额禁止使用 float，请传 str 或 Decimal")
    amount = Decimal(str(value))
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise ValueError(f"金额最多精确到分，收到 {value!r}")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


class Role(str, Enum):
    """参与者角色，决定可执行的操作（对齐宪法第三条职责分离）。"""
    CUSTOMER = "customer"   # 客户：只能发起诉求，不能操作审批或执行
    AGENT = "agent"         # Agent：只能创建草稿并提交审批，不能放行
    APPROVER = "approver"   # 授权人员：触发退款 / 补发 / 升级的最终批准
    SYSTEM = "system"       # 领域服务自身：执行已批准动作


class RefundStatus(str, Enum):
    """退款动作状态机。"""
    DRAFT = "draft"                        # 草稿：Agent 已创建，尚未提交
    PENDING_APPROVAL = "pending_approval"  # 待审批：已提交，等待授权人员
    APPROVED = "approved"                  # 已批准：可执行
    REJECTED = "rejected"                  # 已拒绝：终态
    EXECUTED = "executed"                  # 已执行：终态


class ErrorCode(str, Enum):
    """领域错误码。领域服务抛出的错误不得被上层改写成成功（宪法第六条）。"""
    PERMISSION_DENIED = "PERMISSION_DENIED"
    AMOUNT_NOT_POSITIVE = "AMOUNT_NOT_POSITIVE"
    AMOUNT_EXCEEDS_PAID = "AMOUNT_EXCEEDS_PAID"
    AMOUNT_EXCEEDS_REMAINING = "AMOUNT_EXCEEDS_REMAINING"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    REFUND_NOT_FOUND = "REFUND_NOT_FOUND"
    DECISION_VERSION_MISMATCH = "DECISION_VERSION_MISMATCH"


class DomainError(Exception):
    """领域服务统一异常，携带结构化错误码。"""
    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code.value}] {message}")


# ---------- 命令（强类型参数，对齐宪法第四条） ----------

@dataclass(frozen=True)
class CreateRefundCommand:
    ticket_id: str
    order_id: str
    amount: Decimal
    reason: str
    actor: Role
    idempotency_key: str


@dataclass(frozen=True)
class SubmitCommand:
    refund_id: str
    actor: Role


@dataclass(frozen=True)
class ApproveCommand:
    refund_id: str
    actor: Role
    decision_version: int


@dataclass(frozen=True)
class RejectCommand:
    refund_id: str
    actor: Role
    reason: str


@dataclass(frozen=True)
class ExecuteCommand:
    refund_id: str
    actor: Role


# ---------- 实体 ----------

@dataclass
class RefundAction:
    """退款动作实体，承载权限 / 金额 / 状态 / 版本所需最小字段。"""
    refund_id: str
    ticket_id: str
    order_id: str
    amount: Decimal
    reason: str
    status: RefundStatus
    created_by: Role
    version: int = 1
    decision_version: Optional[int] = None
    executed: bool = False


# ---------- 审计 ----------

@dataclass(frozen=True)
class AuditEvent:
    """不可变审计事件，追加式记录，独立于模型输出。"""
    action: str
    refund_id: str
    actor: Role
    before: Optional[RefundStatus]
    after: RefundStatus
    idempotency_key: Optional[str] = None
