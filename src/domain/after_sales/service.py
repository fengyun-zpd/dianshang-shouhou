"""售后领域服务：确定性职责的单一入口（阶段 1 核心）。

职责（对齐 AGENTS.md 宪法第三、四、六条）：
- 订单核验：租户归属 / 订单存在 / 订单状态可售后；
- 售后资格：结构化政策规则匹配，冲突政策显式转人工（POLICY_CONFLICT）；
- 退款上限：累计已执行退款（含多次部分退款）不超过实付，Decimal 精确到分；
- 工单状态机 + 操作状态机（草稿 → 审批 → 执行 / operation_unknown 对账）；
- 幂等命令：同键同载荷返回原结果，异载荷拒绝；
- operation_unknown：外部结果不明时只能以原操作对账收口，禁止换键重试；
- 审计：追加式不可变事件。

当前使用内存仓储模拟，生产环境应替换为 PostgreSQL 唯一事实源（规划中）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Tuple

from ..idempotency import (
    PENDING_REFUND_ID,
    IdempotencyStore,
    payload_hash,
)
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
    OPERATION_TRANSITIONS,
    Order,
    OrderStatus,
    ReconcileCommand,
    RejectCommand,
    RefundPlan,
    RequestType,
    Role,
    SubmitCommand,
    TERMINAL_OPERATION_STATUSES,
    TICKET_TRANSITIONS,
    TicketStatus,
    parse_money,
)
from .policies import PolicyRule, detect_conflict, match_policies


class AfterSalesService:
    """售后工单与退款动作的确定性领域服务。"""

    def __init__(self, idempotency: Optional[IdempotencyStore] = None):
        self._idempotency = idempotency if idempotency is not None else IdempotencyStore()
        self._orders: dict[str, Order] = {}
        self._policies: list[PolicyRule] = []
        self._tickets: dict[str, AfterSalesTicket] = {}
        self._operations: dict[str, Operation] = {}
        self._refunded_by_order: dict[str, Decimal] = {}  # order_id -> 已执行退款累计
        self._audit: list[AuditEvent] = []
        self._seq = 0

    # ---------- 数据注入（固定随机种子合成数据；生产为 PostgreSQL，规划中） ----------

    def seed_order(self, order: Order) -> None:
        self._orders[order.order_id] = order

    def seed_policy(self, policy: PolicyRule) -> None:
        self._policies.append(policy)

    # ---------- 可恢复持久化（原型）：导出/恢复领域状态（只读/恢复，不改变任何规则） ----------

    def export_state(self) -> dict:
        """导出领域状态，供快照存储与重启恢复（不影响运行；不含任何模型输出）。"""
        return {
            "schema_version": 1,
            "seq": self._seq,
            "orders": dict(self._orders),
            "policies": list(self._policies),
            "tickets": dict(self._tickets),
            "operations": dict(self._operations),
            "refunded": dict(self._refunded_by_order),
            "audit": list(self._audit),
            "idempotency": self._idempotency.export_records(),
        }

    def restore_state(self, state: dict) -> None:
        """从导出状态恢复（仅用于持久化恢复路径；不改变权限/状态机/幂等语义）。"""
        if int(state.get("schema_version", 0)) != 1:
            raise ValueError(f"未知快照 schema_version：{state.get('schema_version')}")
        self._orders = dict(state["orders"])
        self._policies = list(state["policies"])
        self._tickets = dict(state["tickets"])
        self._operations = dict(state["operations"])
        self._refunded_by_order = dict(state["refunded"])
        self._audit = list(state["audit"])
        self._seq = int(state["seq"])
        self._idempotency.import_records(state["idempotency"])

    # ---------- 只读查询 ----------

    def get_ticket(self, ticket_id: str) -> AfterSalesTicket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise AfterSalesError(AfterSalesErrorCode.TICKET_NOT_FOUND, f"工单 {ticket_id} 不存在")
        return ticket

    def get_operation(self, operation_id: str) -> Operation:
        op = self._operations.get(operation_id)
        if op is None:
            raise AfterSalesError(AfterSalesErrorCode.OPERATION_NOT_FOUND, f"操作 {operation_id} 不存在")
        return op

    def operations_of(self, ticket_id: str) -> list[Operation]:
        return [op for op in self._operations.values() if op.ticket_id == ticket_id]

    def refunded_amount(self, order_id: str) -> Decimal:
        return self._refunded_by_order.get(order_id, Decimal("0.00"))

    def audit_log(self) -> list[AuditEvent]:
        return list(self._audit)

    def get_order_by_id(self, tenant_id: str, order_id: str) -> Order:
        """只读订单查询（阶段 2 Agent 证据编排用）：租户归属校验，不暴露跨租户订单。"""
        order = self._orders.get(order_id)
        if order is None:
            raise AfterSalesError(AfterSalesErrorCode.ORDER_NOT_FOUND, f"订单 {order_id} 不存在")
        if order.tenant_id != tenant_id:
            raise AfterSalesError(AfterSalesErrorCode.TENANT_MISMATCH, f"订单 {order_id} 不属于租户 {tenant_id}")
        return order

    def list_customer_tickets(self, tenant_id: str, customer_id: str) -> list[AfterSalesTicket]:
        """只读历史工单查询（证据/审计追溯用）：仅返回同租户下该客户的工单。"""
        return [
            t for t in self._tickets.values()
            if t.tenant_id == tenant_id and t.customer_id == customer_id
        ]

    # ---------- 确定性退款计划（金额由领域规则计算，Agent 只搬运） ----------

    def compute_refund_plan(
        self,
        tenant_id: str,
        order_id: str,
        request_type: RequestType,
        reason_tags: Tuple[str, ...],
    ) -> RefundPlan:
        """确定性退款金额计算。

        适用政策单一且无冲突 → amount = 实付 × refund_ratio（分精度）；
        无适用政策 → POLICY_NOT_FOUND；冲突政策 → POLICY_CONFLICT（均转人工）。
        本方法只读、不落库；create_refund 仍会做金额/幂等/权限校验兜底。
        """
        order = self._require_eligible_order(tenant_id, order_id)
        matched = match_policies(self._policies, tenant_id, request_type, reason_tags, order.days_since_sign)
        if not matched:
            raise AfterSalesError(AfterSalesErrorCode.POLICY_NOT_FOUND, "无适用政策，证据不足，建议转人工")
        if detect_conflict(matched) is not None:
            raise AfterSalesError(AfterSalesErrorCode.POLICY_CONFLICT, "适用政策冲突，需转人工")
        ratio = matched[0].refund_ratio
        amount = (order.paid_amount * ratio).quantize(Decimal("0.01"))
        return RefundPlan(
            amount=amount, refund_ratio=ratio, policy_id=matched[0].policy_id, order_id=order.order_id,
        )

    # ---------- 工单生命周期 ----------

    def create_ticket(self, cmd: CreateTicketCommand) -> AfterSalesTicket:
        # 权限：客户或 Agent 均可录入诉求；审批/执行角色无权建单
        if cmd.actor not in (Role.CUSTOMER, Role.AGENT):
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有客户或 Agent 可以创建工单")

        # 缺参：理由与诉求标签为空 → 无法形成证据链
        if not cmd.reason or not cmd.reason.strip():
            raise AfterSalesError(AfterSalesErrorCode.MISSING_REQUIRED_FIELD, "缺少售后理由")
        if not cmd.reason_tags:
            raise AfterSalesError(AfterSalesErrorCode.MISSING_REQUIRED_FIELD, "缺少诉求标签（无法匹配政策证据）")

        # 订单核验：存在 / 租户归属 / 状态可售后
        order = self._require_eligible_order(cmd.tenant_id, cmd.order_id)

        # 政策证据：无适用政策 → 证据不足转人工；冲突政策 → 转人工
        matched = match_policies(
            self._policies, cmd.tenant_id, cmd.request_type, cmd.reason_tags, order.days_since_sign,
        )
        if not matched:
            raise AfterSalesError(
                AfterSalesErrorCode.POLICY_NOT_FOUND,
                f"无适用政策（tenant={cmd.tenant_id}, tags={cmd.reason_tags}），证据不足，建议转人工",
            )
        if detect_conflict(matched) is not None:
            raise AfterSalesError(
                AfterSalesErrorCode.POLICY_CONFLICT,
                f"多条适用政策退款比例不一致（{len(matched)} 条），冲突需转人工",
            )

        # 原子幂等（任务卡 J）：per-key 锁内 check-then-act；
        # 同键同载荷返回原工单；同键异载荷拒绝；失败释放占位（<pending>）
        with self._idempotency.lock_for(cmd.idempotency_key):
            phash = payload_hash({
                "tenant_id": cmd.tenant_id,
                "order_id": cmd.order_id,
                "customer_id": cmd.customer_id,
                "request_type": cmd.request_type.value,
                "reason": cmd.reason,
                "reason_tags": list(cmd.reason_tags),
            })
            occupied = self._idempotency.get_or_reserve(cmd.idempotency_key, phash)
            if occupied is not None:
                if occupied.payload_hash == phash and occupied.refund_id != PENDING_REFUND_ID:
                    return self.get_ticket(occupied.refund_id)  # 返回原工单
                self._idempotency.release(cmd.idempotency_key)
                raise AfterSalesError(AfterSalesErrorCode.IDEMPOTENCY_CONFLICT, "同键异载荷：工单创建被拒绝")

            try:
                self._seq += 1
                ticket = AfterSalesTicket(
                    ticket_id=f"TKT-{self._seq:05d}",
                    tenant_id=cmd.tenant_id,
                    order_id=cmd.order_id,
                    customer_id=cmd.customer_id,
                    request_type=cmd.request_type,
                    reason=cmd.reason,
                    reason_tags=cmd.reason_tags,
                    status=TicketStatus.OPEN,
                    created_by=cmd.actor,
                )
                self._tickets[ticket.ticket_id] = ticket
                self._idempotency.commit(cmd.idempotency_key, phash, ticket.ticket_id)
                self._record("create_ticket", "ticket", ticket.ticket_id, cmd.actor, None, TicketStatus.OPEN, cmd.idempotency_key)
            except BaseException:
                self._idempotency.release(cmd.idempotency_key)
                raise
            return ticket

    def close_ticket(self, cmd: CloseTicketCommand) -> AfterSalesTicket:
        # 权限：客服 Agent 或领域服务可关单
        if cmd.actor not in (Role.AGENT, Role.SYSTEM):
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有 Agent 或领域服务可以关闭工单")
        ticket = self.get_ticket(cmd.ticket_id)
        if ticket.status == TicketStatus.CLOSED:
            raise AfterSalesError(AfterSalesErrorCode.INVALID_STATE_TRANSITION, "工单已关闭，终态不可再变更")

        # 守卫：存在未决（非终态）操作时不允许关闭
        for op in self.operations_of(cmd.ticket_id):
            if op.status not in TERMINAL_OPERATION_STATUSES:
                raise AfterSalesError(
                    AfterSalesErrorCode.TICKET_HAS_OPEN_OPERATIONS,
                    f"工单存在未决操作 {op.operation_id}（{op.status.value}），禁止关闭",
                )

        # 依据既有操作结果定性（供审计追溯）：有成功退款 → RESOLVED；有拒绝 → REJECTED
        executed = any(op.status == OperationStatus.EXECUTED for op in self.operations_of(cmd.ticket_id))
        rejected = any(op.status == OperationStatus.REJECTED for op in self.operations_of(cmd.ticket_id))
        if executed:
            before = ticket.status
            ticket.resolution = "refunded"
            ticket.status = TicketStatus.RESOLVED
            self._record("resolve_ticket", "ticket", ticket.ticket_id, cmd.actor, before, TicketStatus.RESOLVED)
        elif rejected:
            before = ticket.status
            ticket.resolution = "rejected"
            ticket.status = TicketStatus.REJECTED
            self._record("reject_ticket", "ticket", ticket.ticket_id, cmd.actor, before, TicketStatus.REJECTED)

        before = ticket.status
        self._apply_ticket_transition(ticket, TicketStatus.CLOSED)
        self._record("close_ticket", "ticket", ticket.ticket_id, cmd.actor, before, TicketStatus.CLOSED)
        return ticket

    # ---------- 退款操作：草稿 → 审批 → 执行 ----------

    def create_refund(self, cmd: CreateRefundCommand) -> Operation:
        if cmd.actor != Role.AGENT:
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有 Agent 可以创建退款草稿")
        ticket = self.get_ticket(cmd.ticket_id)
        if ticket.status == TicketStatus.CLOSED:
            raise AfterSalesError(AfterSalesErrorCode.INVALID_STATE_TRANSITION, "工单已关闭，禁止追加退款操作")

        order = self._orders.get(ticket.order_id)
        if order is None:
            raise AfterSalesError(AfterSalesErrorCode.ORDER_NOT_FOUND, f"订单 {ticket.order_id} 不存在")

        # operation_unknown 守卫：同订单存在未知态操作时禁止新建/换键重试
        for op in self.operations_of(ticket.ticket_id):
            if op.status == OperationStatus.UNKNOWN:
                raise AfterSalesError(
                    AfterSalesErrorCode.OPERATION_UNKNOWN_CONFLICT,
                    f"订单 {order.order_id} 存在未知态操作 {op.operation_id}，只能以原幂等键查询对账，禁止换键重试",
                )

        amount = parse_money(cmd.amount)
        if amount <= 0:
            raise AfterSalesError(AfterSalesErrorCode.AMOUNT_NOT_POSITIVE, "退款金额必须为正")
        remaining = order.paid_amount - self.refunded_amount(order.order_id)
        if amount > remaining:
            raise AfterSalesError(
                AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING,
                f"退款 {amount} 超过剩余可退 {remaining}（实付 {order.paid_amount}，已退 {self.refunded_amount(order.order_id)}）",
            )

        # 原子幂等（任务卡 J）：per-key 锁内 check-then-act；
        # 同键同载荷返回原草稿；同键异载荷拒绝；创建失败释放占位
        with self._idempotency.lock_for(cmd.idempotency_key):
            phash = payload_hash({
                "ticket_id": cmd.ticket_id,
                "order_id": order.order_id,
                "amount": str(amount),
                "reason_detail": cmd.reason_detail,
            })
            occupied = self._idempotency.get_or_reserve(cmd.idempotency_key, phash)
            if occupied is not None:
                if occupied.payload_hash == phash and occupied.refund_id != PENDING_REFUND_ID:
                    return self.get_operation(occupied.refund_id)
                self._idempotency.release(cmd.idempotency_key)
                raise AfterSalesError(AfterSalesErrorCode.IDEMPOTENCY_CONFLICT, "同键异载荷：退款草稿被拒绝")

            try:
                self._seq += 1
                op = Operation(
                    operation_id=f"OP-{self._seq:05d}",
                    ticket_id=cmd.ticket_id,
                    tenant_id=ticket.tenant_id,
                    order_id=order.order_id,
                    op_type=OperationType.REFUND,
                    amount=amount,
                    status=OperationStatus.DRAFT,
                    idempotency_key=cmd.idempotency_key,
                    created_by=cmd.actor,
                )
                self._operations[op.operation_id] = op
                self._idempotency.commit(cmd.idempotency_key, phash, op.operation_id)
                self._record("create_refund", "operation", op.operation_id, cmd.actor, None, OperationStatus.DRAFT, cmd.idempotency_key)
            except BaseException:
                self._idempotency.release(cmd.idempotency_key)
                raise
            return op

    def submit(self, cmd: SubmitCommand) -> Operation:
        if cmd.actor != Role.AGENT:
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有 Agent 可以提交审批")
        op, before = self._apply_op_transition(self.get_operation(cmd.operation_id), OperationStatus.PENDING_APPROVAL)
        self._record("submit", "operation", op.operation_id, cmd.actor, before, OperationStatus.PENDING_APPROVAL)
        return op

    def approve(self, cmd: ApproveCommand) -> Operation:
        if cmd.actor != Role.APPROVER:
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有授权人员可以审批")
        op = self.get_operation(cmd.operation_id)
        if cmd.decision_version != op.version:
            raise AfterSalesError(
                AfterSalesErrorCode.DECISION_VERSION_MISMATCH,
                f"决定版本 {cmd.decision_version} 与当前版本 {op.version} 不一致",
            )
        op, before = self._apply_op_transition(op, OperationStatus.APPROVED)
        op.decision_version = cmd.decision_version
        op.version += 1
        self._record("approve", "operation", op.operation_id, cmd.actor, before, OperationStatus.APPROVED)
        return op

    def reject(self, cmd: RejectCommand) -> Operation:
        if cmd.actor != Role.APPROVER:
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有授权人员可以审批")
        op, before = self._apply_op_transition(self.get_operation(cmd.operation_id), OperationStatus.REJECTED)
        self._record("reject", "operation", op.operation_id, cmd.actor, before, OperationStatus.REJECTED, note=cmd.reason)
        return op

    def execute(self, cmd: ExecuteCommand) -> Operation:
        """执行已批准操作。external_result="timeout" 表示外部结果不明 → operation_unknown。"""
        if cmd.actor != Role.SYSTEM:
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有领域服务可以执行已批准动作")
        op = self.get_operation(cmd.operation_id)
        # 只有 approved 操作可执行；unknown 态只能通过 reconcile 对账收口，
        # 禁止以 execute("success") 隐式"重试成功"，防止重复副作用。
        if op.status != OperationStatus.APPROVED:
            raise AfterSalesError(
                AfterSalesErrorCode.INVALID_STATE_TRANSITION,
                f"只有 approved 操作可执行，当前 {op.status.value}（unknown 态只能对账收口）",
            )
        if cmd.external_result == "success":
            op, before = self._apply_op_transition(op, OperationStatus.EXECUTED)
            self._refunded_by_order[op.order_id] = self._refunded_by_order.get(op.order_id, Decimal("0.00")) + (op.amount or Decimal("0.00"))
            op.executed = True
            self._record("execute", "operation", op.operation_id, cmd.actor, before, OperationStatus.EXECUTED)
            return op
        if cmd.external_result == "timeout":
            op, before = self._apply_op_transition(op, OperationStatus.UNKNOWN)
            self._record("execute_timeout", "operation", op.operation_id, cmd.actor, before, OperationStatus.UNKNOWN)
            return op
        raise AfterSalesError(AfterSalesErrorCode.MISSING_REQUIRED_FIELD, f"未知 external_result：{cmd.external_result!r}")

    def reconcile(self, cmd: ReconcileCommand) -> Operation:
        """operation_unknown 对账收口：只能原操作 success→EXECUTED / failed→FAILED。"""
        if cmd.actor != Role.SYSTEM:
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有领域服务可以对账")
        op = self.get_operation(cmd.operation_id)
        if op.status != OperationStatus.UNKNOWN:
            raise AfterSalesError(
                AfterSalesErrorCode.INVALID_STATE_TRANSITION,
                f"只有 unknown 态操作可对账，当前 {op.status.value}",
            )
        if cmd.result == "success":
            op, before = self._apply_op_transition(op, OperationStatus.EXECUTED)
            self._refunded_by_order[op.order_id] = self._refunded_by_order.get(op.order_id, Decimal("0.00")) + (op.amount or Decimal("0.00"))
            op.executed = True
            self._record("reconcile_success", "operation", op.operation_id, cmd.actor, before, OperationStatus.EXECUTED)
            return op
        if cmd.result == "failed":
            op, before = self._apply_op_transition(op, OperationStatus.FAILED)
            self._record("reconcile_failed", "operation", op.operation_id, cmd.actor, before, OperationStatus.FAILED)
            return op
        raise AfterSalesError(AfterSalesErrorCode.MISSING_REQUIRED_FIELD, f"未知对账结果：{cmd.result!r}")

    # ---------- 内部 ----------

    def _require_eligible_order(self, tenant_id: str, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise AfterSalesError(AfterSalesErrorCode.ORDER_NOT_FOUND, f"订单 {order_id} 不存在")
        if order.tenant_id != tenant_id:
            raise AfterSalesError(
                AfterSalesErrorCode.TENANT_MISMATCH,
                f"订单 {order_id} 不属于租户 {tenant_id}（归属 {order.tenant_id}）",
            )
        if order.status == OrderStatus.CLOSED:
            raise AfterSalesError(AfterSalesErrorCode.ORDER_STATUS_NOT_ELIGIBLE, f"订单 {order_id} 已关闭，不可售后")
        return order

    def _apply_ticket_transition(self, ticket: AfterSalesTicket, target: TicketStatus):
        if target not in TICKET_TRANSITIONS[ticket.status]:
            raise AfterSalesError(
                AfterSalesErrorCode.INVALID_STATE_TRANSITION,
                f"非法工单状态迁移 {ticket.status.value} -> {target.value}",
            )
        before = ticket.status
        ticket.status = target
        return ticket, before

    def _apply_op_transition(self, op: Operation, target: OperationStatus):
        if target not in OPERATION_TRANSITIONS[op.status]:
            raise AfterSalesError(
                AfterSalesErrorCode.INVALID_STATE_TRANSITION,
                f"非法操作状态迁移 {op.status.value} -> {target.value}",
            )
        before = op.status
        op.status = target
        return op, before

    def _record(
        self,
        action: str,
        entity_type: str,
        entity_id: str,
        actor: Role,
        before,
        after,
        idem_key: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        self._audit.append(AuditEvent(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
            before=before.value if before is not None else None,
            after=after.value if after is not None else None,
            idempotency_key=idem_key,
            note=note,
        ))
