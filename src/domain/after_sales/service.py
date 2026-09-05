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

import threading
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
from .rules import (  # 确定性规则纯函数（单一规则事实源；内存/PG 后端共用）
    check_operation_transition,
    check_ticket_transition,
    validate_decision_version,
)


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
        # 订单级锁（Task K1）：键=(tenant_id, order_id)。见 _order_lock。
        self._order_guard = threading.RLock()
        self._order_locks: dict[tuple[str, str], threading.Lock] = {}

    # ---------- 数据注入（固定随机种子合成数据；生产为 PostgreSQL，规划中） ----------

    def seed_order(self, order: Order) -> None:
        existing = self._orders.get(order.order_id)
        if existing is not None and existing.tenant_id != order.tenant_id:
            # D1：内存订单存储以 order_id 为键——跨租户同 order_id 不能静默互相覆盖；
            # 多租户同 order_id 共存的权威语义由 PostgreSQL (tenant_id, order_id) 主键承载。
            raise ValueError(
                f"order_id {order.order_id} 已被租户 {existing.tenant_id} 占用；"
                "内存后端拒绝跨租户覆盖（多租户共存请使用 PostgreSQL 唯一事实源）",
            )
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
        """从导出状态恢复（原子：先构造全部局部容器并预检，通过后一次性替换内部状态）。

        语义校验由持久化层 validate_snapshot_state 完成；此处只做结构预检与原子替换，
        任何失败都不会部分改变当前业务状态（K3）。
        """
        expected_keys = {
            "schema_version", "seq", "orders", "policies", "tickets",
            "operations", "refunded", "audit", "idempotency",
        }
        if not isinstance(state, dict) or set(state.keys()) != expected_keys:
            raise ValueError(f"快照顶层字段不符：{set(state.keys()) if isinstance(state, dict) else type(state)}")
        if type(state["schema_version"]) is not int or state["schema_version"] != 1:
            raise ValueError(f"schema_version 必须是 int 且为 1，收到 {state['schema_version']!r}")
        if type(state["seq"]) is not int or state["seq"] < 0:
            raise ValueError(f"seq 必须是 int 且 ≥0，收到 {state['seq']!r}")
        for k in ("orders", "tickets", "operations", "refunded", "idempotency"):
            if not isinstance(state[k], dict):
                raise TypeError(f"快照字段 {k} 必须是 dict，收到 {type(state[k]).__name__}")
        for k in ("policies", "audit"):
            if not isinstance(state[k], list):
                raise TypeError(f"快照字段 {k} 必须是 list，收到 {type(state[k]).__name__}")

        # 全部先构造到局部，构造完成后一次替换（原子）
        new_orders = dict(state["orders"])
        new_policies = list(state["policies"])
        new_tickets = dict(state["tickets"])
        new_operations = dict(state["operations"])
        new_refunded = dict(state["refunded"])
        new_audit = list(state["audit"])
        new_seq = state["seq"]
        new_idem = dict(state["idempotency"])
        self._orders = new_orders
        self._policies = new_policies
        self._tickets = new_tickets
        self._operations = new_operations
        self._refunded_by_order = new_refunded
        self._audit = new_audit
        self._seq = new_seq
        self._idempotency.import_records(new_idem)

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

    def entity_tenant(self, entity_type: str, entity_id: str) -> Optional[str]:
        """审计/查询用：返回实体所属租户（不存在返回 None）。"""
        if entity_type == "ticket":
            t = self._tickets.get(entity_id)
            return t.tenant_id if t else None
        if entity_type == "operation":
            op = self._operations.get(entity_id)
            return op.tenant_id if op else None
        return None

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

        # 原子幂等（任务卡 J）：per-key 锁内 check-then-act；
        # 同键同载荷返回原工单；同键异载荷拒绝；失败释放占位（<pending>）
        # 幂等键租户作用域（D2）：键空间带租户前缀，跨租户同原始 key 互不冲突
        _ik = f"{cmd.tenant_id}:{cmd.idempotency_key}"
        with self._idempotency.lock_for(_ik):
            phash = payload_hash({
                "tenant_id": cmd.tenant_id,
                "order_id": cmd.order_id,
                "customer_id": cmd.customer_id,
                "request_type": cmd.request_type.value,
                "reason": cmd.reason,
                "reason_tags": list(cmd.reason_tags),
            })
            occupied = self._idempotency.get_or_reserve(_ik, phash)
            if occupied is not None:
                if occupied.payload_hash == phash and occupied.refund_id != PENDING_REFUND_ID:
                    return self.get_ticket(occupied.refund_id)  # 返回原工单
                self._idempotency.release(_ik)
                raise AfterSalesError(AfterSalesErrorCode.IDEMPOTENCY_CONFLICT, "同键异载荷：工单创建被拒绝")

            try:
                # 政策证据（锁内、幂等命中之后）：无适用政策 → 证据不足转人工；冲突 → 转人工
                matched = match_policies(
                    self._policies, cmd.tenant_id, cmd.request_type,
                    cmd.reason_tags, order.days_since_sign,
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
                self._idempotency.commit(_ik, phash, ticket.ticket_id)
                self._record("create_ticket", "ticket", ticket.ticket_id, cmd.actor, None, TicketStatus.OPEN, _ik)
            except BaseException:
                self._idempotency.release(_ik)
                raise
            return ticket

    def close_ticket(self, cmd: CloseTicketCommand) -> AfterSalesTicket:
        # 权限：客服 Agent 或领域服务可关单
        if cmd.actor not in (Role.AGENT, Role.SYSTEM):
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有 Agent 或领域服务可以关闭工单")
        ticket = self.get_ticket(cmd.ticket_id)
        with self._order_lock(ticket.tenant_id, ticket.order_id):
            return self._close_ticket_locked(cmd)

    def _close_ticket_locked(self, cmd: CloseTicketCommand) -> AfterSalesTicket:
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
        with self._order_lock(ticket.tenant_id, ticket.order_id):
            return self._create_refund_locked(cmd)

    def _create_refund_locked(self, cmd: CreateRefundCommand) -> Operation:
        """create_refund 的锁内实现（订单级锁；Task K1）。

        一致性顺序：工单状态 → 幂等命中（同键同载荷优先返回原操作，不因金额/unknown 重复误拒）
        → 订单级 unknown 守卫 → 剩余金额校验 → 创建（per-key 锁内 CAS）。
        """
        ticket = self.get_ticket(cmd.ticket_id)
        if ticket.status == TicketStatus.CLOSED:
            raise AfterSalesError(AfterSalesErrorCode.INVALID_STATE_TRANSITION, "工单已关闭，禁止追加退款操作")

        order = self._orders.get(ticket.order_id)
        if order is None:
            raise AfterSalesError(AfterSalesErrorCode.ORDER_NOT_FOUND, f"订单 {ticket.order_id} 不存在")

        amount = parse_money(cmd.amount)
        if amount <= 0:
            raise AfterSalesError(AfterSalesErrorCode.AMOUNT_NOT_POSITIVE, "退款金额必须为正")

        # 幂等键租户作用域（D2）：键空间带工单租户前缀，跨租户同原始 key 互不冲突
        _ik = f"{ticket.tenant_id}:{cmd.idempotency_key}"
        with self._idempotency.lock_for(_ik):
            phash = payload_hash({
                "ticket_id": cmd.ticket_id,
                "order_id": order.order_id,
                "amount": str(amount),
                "reason_detail": cmd.reason_detail,
            })
            occupied = self._idempotency.get_or_reserve(_ik, phash)
            if occupied is not None:
                if occupied.payload_hash == phash and occupied.refund_id != PENDING_REFUND_ID:
                    return self.get_operation(occupied.refund_id)  # 同键同载荷：返回原结果（幂等优先）
                self._idempotency.release(_ik)
                raise AfterSalesError(AfterSalesErrorCode.IDEMPOTENCY_CONFLICT, "同键异载荷：退款草稿被拒绝")

            try:
                # 订单级 unknown 守卫：同订单存在未知态操作时禁止换新键创建（原键已在上方幂等返回）
                if self._has_unknown_on_order(order.order_id):
                    raise AfterSalesError(
                        AfterSalesErrorCode.OPERATION_UNKNOWN_CONFLICT,
                        f"订单 {order.order_id} 存在未知态操作，只能以原幂等键查询对账，禁止换键重试",
                    )
                remaining = order.paid_amount - self.refunded_amount(order.order_id)
                if amount > remaining:
                    raise AfterSalesError(
                        AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING,
                        f"退款 {amount} 超过剩余可退 {remaining}（实付 {order.paid_amount}，已退 {self.refunded_amount(order.order_id)}）",
                    )

                self._seq += 1
                op = Operation(
                    operation_id=f"OP-{self._seq:05d}",
                    ticket_id=cmd.ticket_id,
                    tenant_id=ticket.tenant_id,
                    order_id=order.order_id,
                    op_type=OperationType.REFUND,
                    amount=amount,
                    status=OperationStatus.DRAFT,
                    idempotency_key=_ik,
                    created_by=cmd.actor,
                )
                self._operations[op.operation_id] = op
                self._idempotency.commit(_ik, phash, op.operation_id)
                self._record("create_refund", "operation", op.operation_id, cmd.actor, None, OperationStatus.DRAFT, _ik)
            except BaseException:
                self._idempotency.release(_ik)
                raise
            return op

    def submit(self, cmd: SubmitCommand) -> Operation:
        if cmd.actor != Role.AGENT:
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有 Agent 可以提交审批")
        op, before = self._apply_op_transition(self.get_operation(cmd.operation_id), OperationStatus.PENDING_APPROVAL)
        self._record("submit", "operation", op.operation_id, cmd.actor, before, OperationStatus.PENDING_APPROVAL)
        return op

    def approve(self, cmd: ApproveCommand, decided_by: Optional[str] = None) -> Operation:
        # decided_by：上层认证 principal（供审计身份），领域裁决以角色与版本为准
        if cmd.actor != Role.APPROVER:
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有授权人员可以审批")
        op = self.get_operation(cmd.operation_id)
        validate_decision_version(op.version, cmd.decision_version)   # rules：CAS 语义
        op, before = self._apply_op_transition(op, OperationStatus.APPROVED)
        op.decision_version = cmd.decision_version
        op.version += 1
        self._record("approve", "operation", op.operation_id, cmd.actor, before, OperationStatus.APPROVED)
        return op

    def reject(self, cmd: RejectCommand, decided_by: Optional[str] = None) -> Operation:
        # decided_by：上层认证 principal（供审计身份），领域裁决以角色与版本为准
        if cmd.actor != Role.APPROVER:
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有授权人员可以审批")
        op = self.get_operation(cmd.operation_id)
        validate_decision_version(op.version, cmd.decision_version)   # rules：CAS 语义（同版本竞争恰一成功）
        op, before = self._apply_op_transition(op, OperationStatus.REJECTED)
        op.version += 1  # 拒绝亦推进版本：同 expected_version 的审批/拒绝竞争恰一成功
        self._record("reject", "operation", op.operation_id, cmd.actor, before, OperationStatus.REJECTED, note=cmd.reason)
        return op

    def execute(self, cmd: ExecuteCommand) -> Operation:
        """执行已批准操作（订单级锁内：unknown 检查→容量校验→状态迁移→累计→审计）。

        external_result="timeout" 表示外部结果不明 → operation_unknown（不累计）。
        """
        if cmd.actor != Role.SYSTEM:
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有领域服务可以执行已批准动作")
        seed = self.get_operation(cmd.operation_id)
        with self._order_lock(seed.tenant_id, seed.order_id):
            # 锁内重读，保证与同订单其他执行/对账串行
            op = self.get_operation(cmd.operation_id)
            # 只有 approved 操作可执行；unknown 态只能通过 reconcile 对账收口
            if op.status != OperationStatus.APPROVED:
                raise AfterSalesError(
                    AfterSalesErrorCode.INVALID_STATE_TRANSITION,
                    f"只有 approved 操作可执行，当前 {op.status.value}（unknown 态只能对账收口）",
                )
            if cmd.external_result == "success":
                # 原子容量校验：并发下已执行累计 + 本次不得超实付；超额 → 明确错误码，不迁移不累计
                self._ensure_refund_capacity(op.order_id, op.amount or Decimal("0.00"))
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
        """operation_unknown 对账收口（订单级锁内，容量校验；只能原操作 success→EXECUTED / failed→FAILED）。"""
        if cmd.actor != Role.SYSTEM:
            raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED, "只有领域服务可以对账")
        seed = self.get_operation(cmd.operation_id)
        with self._order_lock(seed.tenant_id, seed.order_id):
            op = self.get_operation(cmd.operation_id)
            if op.status != OperationStatus.UNKNOWN:
                raise AfterSalesError(
                    AfterSalesErrorCode.INVALID_STATE_TRANSITION,
                    f"只有 unknown 态操作可对账，当前 {op.status.value}",
                )
            if cmd.result == "success":
                # 对账成功等价于最终执行：容量校验防止多个 unknown 对账累计越界
                self._ensure_refund_capacity(op.order_id, op.amount or Decimal("0.00"))
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

    # ---------- 订单级一致性（Task K1） ----------

    def _order_lock(self, tenant_id: str, order_id: str) -> threading.Lock:
        """订单级锁：键=(tenant_id, order_id)。临界段覆盖 unknown 检查、剩余金额检查、
        执行/对账成功状态迁移、refunded 累计与对应审计。

        锁顺序约定：需要多把锁的路径一律先取订单锁、后取幂等键锁（create_refund），
        避免死锁；不同订单互不阻塞。
        """
        with self._order_guard:
            return self._order_locks.setdefault((tenant_id, order_id), threading.Lock())

    def _has_unknown_on_order(self, order_id: str) -> bool:
        """订单级 unknown 存在性（任何工单/操作），用于换键创建守卫。"""
        return any(
            op.order_id == order_id and op.status == OperationStatus.UNKNOWN
            for op in self._operations.values()
        )

    def _ensure_refund_capacity(self, order_id: str, amount: Decimal) -> None:
        """执行/对账成功前的原子容量校验（必须在订单锁内调用）。"""
        order = self._orders.get(order_id)
        if order is None:
            raise AfterSalesError(AfterSalesErrorCode.ORDER_NOT_FOUND, f"订单 {order_id} 不存在")
        already = self.refunded_amount(order_id)
        if already + amount > order.paid_amount:
            raise AfterSalesError(
                AfterSalesErrorCode.AMOUNT_EXCEEDS_REMAINING,
                f"执行时退款累计 {already + amount} 将超过实付 {order.paid_amount}"
                f"（已执行 {already}，本次 {amount}），拒绝执行/对账成功；请先核对或人工处理",
            )

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
        check_ticket_transition(ticket.status, target)   # rules：表驱动状态机判定
        before = ticket.status
        ticket.status = target
        return ticket, before

    def _apply_op_transition(self, op: Operation, target: OperationStatus):
        check_operation_transition(op.status, target)    # rules：表驱动状态机判定
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
