"""PG-backed 领域会话（阶段二）：把 AfterSalesService 业务事实落 PostgreSQL 行表并可装载重建。

语义（对齐 AGENTS.md 宪法：PostgreSQL 是业务唯一事实源；checkpoint 只存恢复状态）：
- save(service)：把 service.export_state()（订单/工单/操作/审计 + 幂等记录，及由审计派生的
  审批流水）在 repo.unit_of_work() 内整库清空并重插——单命令后全量镜像写；任何异常整单位
  回滚（无部分提交）；
- load(policies)：从 repo 全表读取 → 装配全新 AfterSalesService（同一套领域规则），可继续
  审批/执行/对账/关单；同幂等键同载荷在重建实例上仍返回原结果（不重复副作用）；
- checkpoint（LangGraph）只存流程恢复状态，与本层无覆盖关系：业务事实一律以 PG 为准，
  可由本会话重建（重启不丢事实）。

诚实边界（与 docs/POSTGRES.md 一致）：
- 本层是"单实例原型级"运行时：领域命令仍在内存裁决、命令后全量镜像写，适合演示/测试/恢复
  演练；跨进程并发一致性、增量 SQL 化编排（领域状态机整体迁移）仍未实现；
- 订单明细 items 不入表（展示数据；退款上限以已入表的 paid_amount 为准）；
- 政策规则（seed_policy）无业务表：save 不持久化，load 后由调用方传入并 seed；
- 内存幂等键为裸 key；落库行带其指向实体的租户；load 遇同 key 跨租户行 → fail-closed。
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Optional, Sequence

from src.domain.after_sales import AfterSalesService
from src.domain.after_sales.models import (
    AfterSalesTicket,
    AuditEvent,
    Operation,
    OperationStatus,
    OperationType,
    Order,
    OrderStatus,
    RequestType,
    TicketStatus,
)
from src.domain.after_sales.policies import PolicyRule
from src.domain.idempotency import IdempotencyRecord
from src.domain.models import Role
from src.repo import (
    AfterSalesRepository,
    ApprovalRow,
    AuditRow,
    IdemRow,
    OperationRow,
    OrderRow,
    TicketRow,
)

_ID_RE = re.compile(r"^(?:TKT|OP)-(\d{5})$")


def _id_seq(entity_ids: Sequence[str]) -> int:
    """从现存实体 id 推导领域 seq（TKT-00001 → 1）。id 全程单调递增；
    即使原 seq 有失败空洞，load 后从 max(id) 续号也不会与现存 id 冲突。"""
    seq = 0
    for eid in entity_ids:
        m = _ID_RE.match(eid)
        if m:
            seq = max(seq, int(m.group(1)))
    return seq


# ---------- 行 ↔ 领域对象（保真编解码） ----------

def order_to_row(o: Order) -> OrderRow:
    return OrderRow(o.tenant_id, o.order_id, o.customer_id, o.status.value,
                    o.paid_amount.quantize(Decimal("0.01")), o.days_since_sign)


def order_from_row(r: OrderRow) -> Order:
    # 订单明细 items 不入表（展示数据）；裁决字段全部保留
    return Order(order_id=r.order_id, tenant_id=r.tenant_id, customer_id=r.customer_id,
                 status=OrderStatus(r.status), paid_amount=r.paid_amount,
                 items=[], days_since_sign=r.days_since_sign)


def ticket_to_row(t: AfterSalesTicket) -> TicketRow:
    return TicketRow(t.tenant_id, t.ticket_id, t.order_id, t.customer_id,
                     t.request_type.value, t.reason, t.status.value, t.resolution,
                     t.version, t.created_by.value,
                     json.dumps(list(t.reason_tags), ensure_ascii=False))


def ticket_from_row(r: TicketRow) -> AfterSalesTicket:
    tags: tuple = tuple(json.loads(r.reason_tags)) if r.reason_tags else ()
    return AfterSalesTicket(ticket_id=r.ticket_id, tenant_id=r.tenant_id,
                            order_id=r.order_id, customer_id=r.customer_id,
                            request_type=RequestType(r.request_type), reason=r.reason,
                            reason_tags=tags, status=TicketStatus(r.status),
                            created_by=Role(r.created_by), resolution=r.resolution,
                            version=r.version)


def operation_to_row(op: Operation) -> OperationRow:
    return OperationRow(op.tenant_id, op.operation_id, op.ticket_id, op.order_id,
                        op.op_type.value, op.amount, op.status.value,
                        op.idempotency_key, op.created_by.value, op.version,
                        op.decision_version, op.executed)


def operation_from_row(r: OperationRow) -> Operation:
    return Operation(operation_id=r.operation_id, ticket_id=r.ticket_id,
                     tenant_id=r.tenant_id, order_id=r.order_id,
                     op_type=OperationType(r.op_type), amount=r.amount,
                     status=OperationStatus(r.status), idempotency_key=r.idempotency_key,
                     created_by=Role(r.created_by), version=r.version,
                     decision_version=r.decision_version, executed=r.executed)


def audit_from_row(r: AuditRow) -> AuditEvent:
    return AuditEvent(action=r.action, entity_type=r.entity_type, entity_id=r.entity_id,
                      actor=Role(r.actor), before=r.before_state, after=r.after_state,
                      idempotency_key=r.idempotency_key, note=r.note)


class PgBackedSession:
    """把领域服务状态整库镜像到 Repository（PG/memory 均可），并可从行表重建服务。

    repository 为 AfterSalesRepository 实现；PG 场景传入 PostgresAfterSalesRepository。
    """

    def __init__(self, repository: AfterSalesRepository):
        self._repo = repository

    # ---------- 落库 ----------

    def save(self, service: AfterSalesService) -> None:
        """整库镜像写：清空 → 按领域状态重插全部行（单事务；异常零残留）。"""
        state = service.export_state()
        if state["schema_version"] != 1:
            raise ValueError(f"不支持的 schema_version：{state['schema_version']!r}")

        orders = list(state["orders"].values())
        tickets = list(state["tickets"].values())
        operations = list(state["operations"].values())
        audit = list(state["audit"])
        idem = dict(state["idempotency"])  # key -> IdempotencyRecord

        # idem/audit 行的租户从指向实体（工单/操作/订单）推断（领域审计事件本身不带租户字段）
        entity_tenant: dict[str, str] = {}
        for o in orders:
            entity_tenant[o.order_id] = o.tenant_id
        for t in tickets:
            entity_tenant[t.ticket_id] = t.tenant_id
        for op in operations:
            entity_tenant[op.operation_id] = op.tenant_id
        idem_rows: list[IdemRow] = []
        for key, rec in idem.items():
            tenant = entity_tenant.get(rec.refund_id)
            if tenant is None:
                raise ValueError(
                    f"幂等记录 {key} 指向实体 {rec.refund_id} 不存在（无法确定租户），拒绝落库")
            idem_rows.append(IdemRow(tenant, key, rec.payload_hash, rec.refund_id))

        # 审批流水：由审计中的 approve/reject 事件派生（decided_version 取自操作对象）
        ops_by_id = {op.operation_id: op for op in operations}
        approval_rows: list[ApprovalRow] = []
        for e in audit:
            if e.action not in ("approve", "reject"):
                continue
            op = ops_by_id.get(e.entity_id)
            if op is None:
                raise ValueError(f"审批审计 {e.action} 指向不存在操作 {e.entity_id}，拒绝落库")
            decision = "approved" if e.action == "approve" else "rejected"
            decided_version = op.decision_version if op.decision_version is not None else op.version
            approval_rows.append(ApprovalRow(op.tenant_id, op.operation_id, decision,
                                             e.note, e.actor.value, decided_version))

        with self._repo.unit_of_work():
            self._repo.clear_all()
            for o in orders:
                self._repo.insert_order(order_to_row(o))
            for t in tickets:
                self._repo.insert_ticket(ticket_to_row(t))
            for op in operations:
                self._repo.insert_operation(operation_to_row(op))
            for a in approval_rows:
                self._repo.insert_approval(a)
            for e in audit:
                tenant = entity_tenant.get(e.entity_id)
                if tenant is None:
                    raise ValueError(
                        f"审计事件 {e.action} 指向实体 {e.entity_type}:{e.entity_id} 不存在"
                        "（无法确定租户），拒绝落库")
                self._repo.insert_audit(AuditRow(
                    tenant, e.action, e.entity_type, e.entity_id, e.actor.value,
                    e.before, e.after, e.idempotency_key, e.note))
            for r in idem_rows:
                self._repo.insert_idem(r)

    # ---------- 装载重建 ----------

    def load(self, policies: Sequence[PolicyRule] = ()) -> AfterSalesService:
        """从 Repository 全表读取并装配全新领域服务（load 后需由调用方按需 seed policies）。"""
        orders = self._repo.list_orders()
        tickets = self._repo.list_tickets()
        operations = self._repo.list_operations()
        audit_rows = self._repo.list_audit()
        idem_rows = self._repo.list_idem()

        state: dict = {
            "schema_version": 1,
            "seq": _id_seq([t.ticket_id for t in tickets]
                           + [op.operation_id for op in operations]),
            "orders": {r.order_id: order_from_row(r) for r in orders},
            "policies": list(policies),
            "tickets": {r.ticket_id: ticket_from_row(r) for r in tickets},
            "operations": {r.operation_id: operation_from_row(r) for r in operations},
            "refunded": {},
            "audit": [audit_from_row(r) for r in audit_rows],
            "idempotency": {},
        }
        # refunded 累计 = 已执行操作求和（与领域 validate 规则一致，不信任镜像外的派生值）
        for op in state["operations"].values():
            if op.status == OperationStatus.EXECUTED and op.amount is not None:
                state["refunded"][op.order_id] = (
                    state["refunded"].get(op.order_id, Decimal("0.00")) + op.amount
                ).quantize(Decimal("0.01"))
        # 幂等记录恢复：裸 key → 记录；同 key 跨租户行 → fail-closed（防静默覆盖）
        for r in idem_rows:
            key = r.idem_key
            if key in state["idempotency"]:
                raise ValueError(f"幂等键 {key} 存在跨租户行，拒绝装载（防静默覆盖）")
            state["idempotency"][key] = IdempotencyRecord(payload_hash=r.payload_hash,
                                                          refund_id=r.refund_id)

        service = AfterSalesService()
        service.restore_state(state)
        return service
