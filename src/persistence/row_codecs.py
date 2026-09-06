"""PG 行 ↔ 领域对象编解码（保真 codec），供 PgCommandService/PgCommandAdapter 生产路径使用。

本模块自 `pg_backed.py` 的整库镜像原型中拆出：原 `PgBackedSession`（save/load 整库镜像 +
clear_all 重插）不在生产命令路径，已删除；codec 纯函数被生产路径依赖，故独立保留于此：

- from_row：把 Repository 行（OrderRow/TicketRow/OperationRow/PolicyRow/OrderItemRow/AuditRow）
  解码为领域对象（Order/AfterSalesTicket/Operation/PolicyRule/OrderItem/AuditEvent）；
- to_row：把领域对象编码为行（落库用）；
- 政策无生效日期语义：policies.effective_from 使用占位常量 `_POLICY_EPOCH`（长期有效起点，
  第八阶段 RAG 政策生效期引入后演进）；
- 订单明细 items 不入 orders 行：由 order_items 表按 (tenant, order) 组装（Repository 语义）；
- 内存幂等键为带租户前缀 key；落库行带其指向实体的租户（见 repo/interfaces.py IdemRow 说明）。

本模块只含纯函数与常量，不持有任何状态、不引用领域服务本身。
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Sequence

from src.domain.after_sales.models import (
    AfterSalesTicket,
    AuditEvent,
    Operation,
    OperationStatus,
    OperationType,
    Order,
    OrderItem,
    OrderStatus,
    RequestType,
    TicketStatus,
)
from src.domain.after_sales.policies import PolicyRule
from src.domain.models import Role
from src.repo import (
    AuditRow,
    OperationRow,
    OrderItemRow,
    OrderRow,
    PolicyRow,
    TicketRow,
)

_ID_RE = re.compile(r"^(?:TKT|OP)-(\d{5})$")

# V1 政策无生效日期语义：占位为长期有效起点（第八阶段政策版本/生效期引入后演进）
_POLICY_EPOCH = "1970-01-01"


def _id_seq(entity_ids: Sequence[str]) -> int:
    """从现存实体 id 推导领域 seq（TKT-00001 → 1）。id 全程单调递增；
    即使原 seq 有失败空洞，从 max(id) 续号也不会与现存 id 冲突。"""
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
    # 订单裁决字段自 orders 行恢复；明细 items 由 order_items 表恢复（按 (tenant, order) 组装）
    return Order(order_id=r.order_id, tenant_id=r.tenant_id, customer_id=r.customer_id,
                 status=OrderStatus(r.status), paid_amount=r.paid_amount,
                 items=[], days_since_sign=r.days_since_sign)


# ---------- 0004 数据面：政策与订单明细（D8：完整持久化与恢复） ----------

def policy_to_row(p: PolicyRule) -> PolicyRow:
    return PolicyRow(p.tenant_id, p.policy_id, p.request_type.value,
                     json.dumps(list(p.reason_tags), ensure_ascii=False),
                     p.window_days, p.refund_ratio, _POLICY_EPOCH, 1)


def policy_from_row(r: PolicyRow) -> PolicyRule:
    return PolicyRule(policy_id=r.policy_id, tenant_id=r.tenant_id,
                      request_type=RequestType(r.request_type),
                      reason_tags=tuple(json.loads(r.reason_tags)),
                      window_days=r.window_days, refund_ratio=r.refund_ratio)


def order_item_to_row(o: Order, item: OrderItem) -> OrderItemRow:
    return OrderItemRow(o.tenant_id, o.order_id, item.sku, item.name,
                        item.quantity, item.unit_price)


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
