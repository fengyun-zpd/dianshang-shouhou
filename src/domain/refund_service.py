"""退款领域服务：确定性职责的单一入口。

对齐宪法第三、四、六条：
- Agent 只能创建草稿并提交审批，不能放行；
- 授权人员触发审批（approve / reject）；
- 领域服务自身执行已批准动作（execute）；
- 金额 / 权限 / 状态 / 幂等 / 审计全部确定性完成，错误码不被改写。

说明：当前使用内存仓储模拟，生产环境应替换为 PostgreSQL 唯一事实源（规划中）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .idempotency import IdempotencyStore, payload_hash
from .models import (
    ApproveCommand,
    AuditEvent,
    CreateRefundCommand,
    DomainError,
    ErrorCode,
    ExecuteCommand,
    RefundAction,
    RefundStatus,
    RejectCommand,
    Role,
    SubmitCommand,
    parse_money,
)

# 合法状态迁移表：终态（REJECTED / EXECUTED）无出边。
VALID_TRANSITIONS: dict[RefundStatus, set[RefundStatus]] = {
    RefundStatus.DRAFT: {RefundStatus.PENDING_APPROVAL, RefundStatus.REJECTED},
    RefundStatus.PENDING_APPROVAL: {RefundStatus.APPROVED, RefundStatus.REJECTED},
    RefundStatus.APPROVED: {RefundStatus.EXECUTED},
    RefundStatus.REJECTED: set(),
    RefundStatus.EXECUTED: set(),
}


class RefundService:
    def __init__(self, idempotency: Optional[IdempotencyStore] = None):
        self._idempotency = idempotency if idempotency is not None else IdempotencyStore()
        self._orders: dict[str, Decimal] = {}        # order_id -> 实付金额
        self._refunds: dict[str, RefundAction] = {}  # refund_id -> 动作
        self._refunded: dict[str, Decimal] = {}      # ticket_id -> 已退金额
        self._audit: list[AuditEvent] = []
        self._seq = 0

    # ---------- 数据注入（生产应来自 PostgreSQL，此处为内存模拟） ----------
    def seed_order(self, order_id: str, paid_amount) -> None:
        """注入订单实付金额（固定种子合成数据）。"""
        self._orders[order_id] = parse_money(paid_amount)

    # ---------- 只读查询 ----------
    def get_refund(self, refund_id: str) -> RefundAction:
        action = self._refunds.get(refund_id)
        if action is None:
            raise DomainError(ErrorCode.REFUND_NOT_FOUND, f"退款 {refund_id} 不存在")
        return action

    def audit_log(self) -> list[AuditEvent]:
        return list(self._audit)

    # ---------- 命令处理 ----------
    def create_draft(self, cmd: CreateRefundCommand) -> RefundAction:
        # 1. 权限：只有 Agent 能创建草稿
        if cmd.actor != Role.AGENT:
            raise DomainError(ErrorCode.PERMISSION_DENIED, "只有 Agent 可以创建退款草稿")

        # 2. 金额校验（确定性边界）
        amount = parse_money(cmd.amount)
        if amount <= 0:
            raise DomainError(ErrorCode.AMOUNT_NOT_POSITIVE, "退款金额必须为正")
        paid = self._orders.get(cmd.order_id)
        if paid is None:
            raise DomainError(ErrorCode.REFUND_NOT_FOUND, f"订单 {cmd.order_id} 不存在")
        if amount > paid:
            raise DomainError(ErrorCode.AMOUNT_EXCEEDS_PAID, f"退款 {amount} 超过实付 {paid}")
        already = self._refunded.get(cmd.ticket_id, Decimal("0.00"))
        if amount > paid - already:
            raise DomainError(
                ErrorCode.AMOUNT_EXCEEDS_REMAINING,
                f"累计退款 {amount + already} 将超过实付 {paid}",
            )

        # 3. 幂等：同键同载荷返回原结果；同键异载荷拒绝
        phash = payload_hash({
            "ticket_id": cmd.ticket_id,
            "order_id": cmd.order_id,
            "amount": str(amount),
            "reason": cmd.reason,
        })
        existing = self._idempotency.get(cmd.idempotency_key)
        if existing is not None:
            if existing.payload_hash == phash:
                return self.get_refund(existing.refund_id)
            raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT, f"幂等键 {cmd.idempotency_key} 载荷不一致")

        # 4. 创建草稿
        self._seq += 1
        refund_id = f"RF-{self._seq:05d}"
        action = RefundAction(
            refund_id=refund_id,
            ticket_id=cmd.ticket_id,
            order_id=cmd.order_id,
            amount=amount,
            reason=cmd.reason,
            status=RefundStatus.DRAFT,
            created_by=cmd.actor,
        )
        self._refunds[refund_id] = action
        self._idempotency.register(cmd.idempotency_key, phash, refund_id)
        self._record("create_draft", action, cmd.actor, None, RefundStatus.DRAFT, cmd.idempotency_key)
        return action

    def submit_for_approval(self, cmd: SubmitCommand) -> RefundAction:
        if cmd.actor != Role.AGENT:
            raise DomainError(ErrorCode.PERMISSION_DENIED, "只有 Agent 可以提交审批")
        action, before = self._apply_transition(cmd.refund_id, RefundStatus.PENDING_APPROVAL)
        self._record("submit_for_approval", action, cmd.actor, before, RefundStatus.PENDING_APPROVAL)
        return action

    def approve(self, cmd: ApproveCommand) -> RefundAction:
        if cmd.actor != Role.APPROVER:
            raise DomainError(ErrorCode.PERMISSION_DENIED, "只有授权人员可以审批")
        action = self.get_refund(cmd.refund_id)
        # 审批决定必须带版本号，且与当前版本一致（乐观锁）
        if cmd.decision_version != action.version:
            raise DomainError(
                ErrorCode.DECISION_VERSION_MISMATCH,
                f"决定版本 {cmd.decision_version} 与当前 {action.version} 不一致",
            )
        action, before = self._apply_transition(cmd.refund_id, RefundStatus.APPROVED)
        action.decision_version = cmd.decision_version
        action.version += 1
        self._record("approve", action, cmd.actor, before, RefundStatus.APPROVED)
        return action

    def reject(self, cmd: RejectCommand) -> RefundAction:
        if cmd.actor != Role.APPROVER:
            raise DomainError(ErrorCode.PERMISSION_DENIED, "只有授权人员可以审批")
        action, before = self._apply_transition(cmd.refund_id, RefundStatus.REJECTED)
        self._record("reject", action, cmd.actor, before, RefundStatus.REJECTED)
        return action

    def execute(self, cmd: ExecuteCommand) -> RefundAction:
        if cmd.actor != Role.SYSTEM:
            raise DomainError(ErrorCode.PERMISSION_DENIED, "只有领域服务可以执行已批准动作")
        action, before = self._apply_transition(cmd.refund_id, RefundStatus.EXECUTED)
        self._refunded[action.ticket_id] = self._refunded.get(action.ticket_id, Decimal("0.00")) + action.amount
        action.executed = True
        self._record("execute", action, cmd.actor, before, RefundStatus.EXECUTED)
        return action

    # ---------- 内部 ----------
    def _apply_transition(self, refund_id: str, target: RefundStatus):
        action = self.get_refund(refund_id)
        if target not in VALID_TRANSITIONS[action.status]:
            raise DomainError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"非法状态迁移 {action.status.value} -> {target.value}",
            )
        before = action.status
        action.status = target
        return action, before

    def _record(self, action_name: str, refund: RefundAction, actor: Role, before, after: RefundStatus, idem_key: Optional[str] = None):
        self._audit.append(AuditEvent(
            action=action_name,
            refund_id=refund.refund_id,
            actor=actor,
            before=before,
            after=after,
            idempotency_key=idem_key,
        ))
