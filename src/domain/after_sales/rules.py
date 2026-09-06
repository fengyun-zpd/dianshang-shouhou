"""确定性业务规则（纯函数，无副作用、可单测、不依赖 LLM/存储）。

抽取自 AfterSalesService 命令路径（src/domain/after_sales/service.py），作为内存服务与
PG-first 命令服务共用的单一规则事实源：
- 工单/订单租户匹配、订单资格；
- 客户与订单匹配（客户只能为其本人订单发起售后）；
- 角色权限（写命令）；
- 退款金额为正、订单累计退款容量；
- 工单/操作/审批状态迁移（表驱动）；
- 审批/拒绝 expected_version（CAS 语义）；
- 幂等载荷一致性判定；
- operation_unknown 收口守卫；
- 关单定性。

约定：校验失败抛 AfterSalesError（稳定错误码 + 原消息文本）；模型/Agent 输出不得覆盖
本模块的判定结果（错误码不得被改写成成功）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional

from src.domain.after_sales.models import (
    AfterSalesError,
    AfterSalesErrorCode,
    OperationStatus,
    OPERATION_TRANSITIONS,
    TICKET_TRANSITIONS,
    TicketStatus,
)
from src.domain.models import Role


# ---------- 角色权限（写命令强类型门禁） ----------

def require_role(actor: Role, allowed: tuple[Role, ...], message: str) -> None:
    """actor 不在 allowed 中 → PERMISSION_DENIED。"""
    if actor not in allowed:
        raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, message)


# ---------- 订单访问与租户/客户匹配 ----------

def validate_order_access(order, tenant_id: str) -> None:
    """订单存在（调用方传入 None 视为不存在）/租户归属/状态可售后。"""
    if order is None:
        raise AfterSalesError(AfterSalesErrorCode.ORDER_NOT_FOUND, "订单不存在")
    if order.tenant_id != tenant_id:
        raise AfterSalesError(
            AfterSalesErrorCode.TENANT_MISMATCH,
            f"订单 {order.order_id} 不属于租户 {tenant_id}（归属 {order.tenant_id}）",
        )
    if order.status.value == "closed":
        raise AfterSalesError(AfterSalesErrorCode.ORDER_STATUS_NOT_ELIGIBLE,
                              f"订单 {order.order_id} 已关闭，不可售后")


def validate_customer_order_match(order, customer_id: str) -> None:
    """客户只能为自己的订单发起售后（资源级授权；防伪造 customer_id 借用他客户订单）。"""
    if order is None or order.customer_id != customer_id:
        raise AfterSalesError(
            AfterSalesErrorCode.PERMISSION_DENIED,
            "客户只能为自己的订单发起售后",
        )


def validate_ticket_tenant(ticket, tenant_id: str) -> None:
    """工单读取/写路径的租户门禁。"""
    if ticket is None:
        raise AfterSalesError(AfterSalesErrorCode.TICKET_NOT_FOUND, "工单不存在")
    if ticket.tenant_id != tenant_id:
        raise AfterSalesError(
            AfterSalesErrorCode.TENANT_MISMATCH,
            f"工单 {ticket.ticket_id} 不属于租户 {tenant_id}（归属 {ticket.tenant_id}）",
        )


# ---------- 金额与容量 ----------

def validate_amount_positive(amount: Decimal) -> None:
    if amount <= 0:
        raise AfterSalesError(AfterSalesErrorCode.AMOUNT_NOT_POSITIVE, "退款金额必须为正")


def validate_refund_capacity(order_paid: Decimal, executed_sum: Decimal, amount: Decimal) -> None:
    """累计已执行 + 本次 ≤ 实付；超额 → AMOUNT_EXCEEDS_REMAINING（不迁移不累计）。"""
    if executed_sum + amount > order_paid:
        raise AfterSalesError(
            AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING,
            f"执行时退款累计 {executed_sum + amount} 将超过实付 {order_paid}"
            f"（已执行 {executed_sum}，本次 {amount}），拒绝执行/对账成功；请先核对或人工处理",
        )


# ---------- 状态迁移（表驱动） ----------

def check_transition(current, target, transitions: dict, kind: str = "") -> None:
    """查状态机表；非法迁移 → INVALID_STATE_TRANSITION。current/target 为枚举值。"""
    if target not in transitions[current]:
        raise AfterSalesError(
            AfterSalesErrorCode.INVALID_STATE_TRANSITION,
            f"非法{kind}状态迁移 {current.value if hasattr(current, 'value') else current}"
            f" -> {target.value if hasattr(target, 'value') else target}",
        )


def check_operation_transition(current: OperationStatus, target: OperationStatus) -> None:
    check_transition(current, target, OPERATION_TRANSITIONS, kind="操作")


def check_ticket_transition(current: TicketStatus, target: TicketStatus) -> None:
    check_transition(current, target, TICKET_TRANSITIONS, kind="工单")


# ---------- 审批/拒绝 expected_version（CAS） ----------

def validate_decision_version(current_version: int, expected_version: int) -> None:
    if expected_version != current_version:
        raise AfterSalesError(
            AfterSalesErrorCode.DECISION_VERSION_MISMATCH,
            f"决定版本 {expected_version} 与当前版本 {current_version} 不一致",
        )


# ---------- 幂等载荷一致性 ----------

def idempotency_hit_ok(occupied, payload_hash: str) -> bool:
    """同键命中：载荷一致且为已提交记录（非 <pending>）→ 返回原结果（幂等优先）。"""
    return occupied is not None and occupied.payload_hash == payload_hash and occupied.refund_id != "<pending>"


# ---------- operation_unknown 守卫 ----------

def has_unknown_on_order(operations: Iterable, order_id: str) -> bool:
    """订单（任意工单/操作）存在 unknown 态 → 禁止换新键创建；只能原键查询对账。"""
    return any(getattr(op, "order_id", None) == order_id
               and op.status == OperationStatus.UNKNOWN for op in operations)


# ---------- 关单定性（审计/状态依据） ----------

def ticket_close_qualification(has_executed: bool, has_rejected: bool) -> tuple[Optional[str], Optional[TicketStatus]]:
    """依据既有操作结果定性：有成功退款 → RESOLVED(refunded)；有拒绝 → REJECTED(rejected)。"""
    if has_executed:
        return "refunded", TicketStatus.RESOLVED
    if has_rejected:
        return "rejected", TicketStatus.REJECTED
    return None, None
