"""快照严格结构/语义校验（任务卡 J）。

输入：已解码的领域状态（与 service.export_state() 同形：dataclass 对象）。
规则：顶层键集合精确、引用关系（工单→订单、操作→工单/订单）、租户一致、
退款累计==已执行求和且≤实付、Decimal 有限且非负/正、seq 单调、状态组合合法、
审计与幂等记录引用存在、无 pending 幂等占位。任何违规统一抛 SnapshotCorruptionError。
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Iterable, Optional

from src.domain.after_sales.models import (
    AfterSalesTicket,
    AuditEvent,
    Operation,
    OperationStatus,
    Order,
    OrderItem,
    TicketStatus,
)
from src.domain.after_sales.policies import PolicyRule
from src.domain.idempotency import IdempotencyRecord
from src.persistence.store import SnapshotCorruptionError

_REQUIRED_TOP_KEYS = {
    "schema_version", "seq", "orders", "policies", "tickets",
    "operations", "refunded", "audit", "idempotency",
}

_TERMINAL_OPERATION = {OperationStatus.REJECTED, OperationStatus.EXECUTED,
                       OperationStatus.FAILED}

_ID_TAIL = re.compile(r"(?:TKT|OP)-(\d+)$")


def _bad(message: str) -> SnapshotCorruptionError:
    return SnapshotCorruptionError(f"快照校验失败：{message}")


def _check_amount(name: str, value, *, positive: bool = False) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal):
        raise _bad(f"{name} 不是 Decimal：{value!r}")
    if not value.is_finite():
        raise _bad(f"{name} 必须为有限小数（拒绝 NaN/Infinity）：{value!r}")
    if value < 0 or (positive and value <= 0):
        raise _bad(f"{name} 金额非法（{'须为正' if positive else '不可为负'}）：{value!r}")


def _seq_of(entity_id: str) -> Optional[int]:
    m = _ID_TAIL.search(entity_id)
    return int(m.group(1)) if m else None


def validate_snapshot_state(state: dict) -> bool:
    """完整校验；违规抛 SnapshotCorruptionError；通过返回 True。"""
    if not isinstance(state, dict):
        raise _bad("快照不是字典")
    keys = set(state.keys())
    if keys != _REQUIRED_TOP_KEYS:
        extra = keys - _REQUIRED_TOP_KEYS
        missing = _REQUIRED_TOP_KEYS - keys
        raise _bad(f"顶层字段集合不符（多出 {sorted(extra)}，缺失 {sorted(missing)}）")

    # ---- 顶层类型严格（K3）：schema_version/seq 仅合法 int，拒小数截断/str/bool ----
    for numeric_key in ("schema_version", "seq"):
        value = state[numeric_key]
        if type(value) is not int or isinstance(value, bool):
            raise _bad(f"{numeric_key} 必须是 int（收到 {type(value).__name__}：{value!r}，拒绝截断/类型转换）")
    if int(state["schema_version"]) != 1:
        raise _bad(f"schema_version 必须为 1，收到 {state['schema_version']}")
    dict_keys = ("orders", "tickets", "operations", "refunded", "idempotency")
    for k in dict_keys:
        if not isinstance(state[k], dict):
            raise _bad(f"顶层字段 {k} 必须是 dict，收到 {type(state[k]).__name__}")
    for k in ("policies", "audit"):
        if not isinstance(state[k], list):
            raise _bad(f"顶层字段 {k} 必须是 list，收到 {type(state[k]).__name__}")

    orders: dict[str, Order] = state["orders"]
    policies: list[PolicyRule] = state["policies"]
    tickets: dict[str, AfterSalesTicket] = state["tickets"]
    operations: dict[str, Operation] = state["operations"]
    refunded: dict[str, Decimal] = state["refunded"]
    audit: list[AuditEvent] = state["audit"]
    idem: dict[str, IdempotencyRecord] = state["idempotency"]
    seq = state["seq"]
    if seq < 0:
        raise _bad(f"seq 不可为负：{seq}")

    # ---- 订单 ----
    max_seq = seq
    for oid, order in orders.items():
        if not isinstance(order, Order):
            raise _bad(f"订单 {oid} 结构错误")
        _check_amount(f"订单 {oid} paid_amount", order.paid_amount)
        if order.days_since_sign < 0:
            raise _bad(f"订单 {oid} days_since_sign 不可为负")
        for item in order.items:
            if not isinstance(item, OrderItem):
                raise _bad(f"订单 {oid} 明细结构错误")
            _check_amount(f"订单 {oid} 明细单价", item.unit_price)
            if item.quantity <= 0:
                raise _bad(f"订单 {oid} 明细数量须为正：{item.quantity}")
        sid = _seq_of(oid)
        if sid is not None:
            max_seq = max(max_seq, sid)

    # ---- 政策 ----
    for p in policies:
        if not isinstance(p, PolicyRule):
            raise _bad("政策结构错误")
        if not (Decimal("0") <= p.refund_ratio <= Decimal("1")):
            raise _bad(f"政策 {p.policy_id} refund_ratio 超出 [0,1]")
        if p.window_days < 0:
            raise _bad(f"政策 {p.policy_id} window_days 不可为负")
        if not isinstance(p.reason_tags, tuple) or not all(isinstance(t, str) for t in p.reason_tags):
            raise _bad(f"政策 {p.policy_id} reason_tags 必须是 str 元组")

    # ---- 工单 / 操作引用与租户 ----
    order_tenant: dict[str, str] = {oid: o.tenant_id for oid, o in orders.items()}
    ticket_tenant: dict[str, str] = {}
    for tid, ticket in tickets.items():
        if not isinstance(ticket, AfterSalesTicket):
            raise _bad(f"工单 {tid} 结构错误")
        if not isinstance(ticket.reason_tags, tuple) or not all(isinstance(t, str) for t in ticket.reason_tags):
            raise _bad(f"工单 {tid} reason_tags 必须是 str 元组")
        if ticket.order_id not in orders:
            raise _bad(f"工单 {tid} 引用不存在的订单 {ticket.order_id}")
        if ticket.tenant_id != order_tenant[ticket.order_id]:
            raise _bad(f"工单 {tid} 租户 {ticket.tenant_id} 与订单租户不一致")
        ticket_tenant[tid] = ticket.tenant_id
        sid = _seq_of(tid)
        if sid is not None:
            max_seq = max(max_seq, sid)

    executed_sum: dict[str, Decimal] = {}
    for oid, op in operations.items():
        if not isinstance(op, Operation):
            raise _bad(f"操作 {oid} 结构错误")
        if op.ticket_id not in tickets:
            raise _bad(f"操作 {oid} 引用不存在的工单 {op.ticket_id}")
        t = tickets[op.ticket_id]
        if op.order_id != t.order_id:
            raise _bad(f"操作 {oid} 的 order_id 与所属工单不一致")
        if op.tenant_id != t.tenant_id:
            raise _bad(f"操作 {oid} 租户与所属工单不一致")
        _check_amount(f"操作 {oid} amount", op.amount, positive=(op.op_type == "refund"))
        if op.executed != (op.status == OperationStatus.EXECUTED):
            raise _bad(f"操作 {oid} executed 与状态不一致（{op.status.value}）")
        if op.status == OperationStatus.EXECUTED:
            executed_sum[t.order_id] = executed_sum.get(t.order_id, Decimal("0")) + (op.amount or Decimal("0"))
        if op.decision_version is not None and op.status not in (
                OperationStatus.APPROVED, OperationStatus.EXECUTED, OperationStatus.UNKNOWN,
                OperationStatus.FAILED):
            raise _bad(f"操作 {oid} 决定版本存在但状态不合法")
        sid = _seq_of(oid)
        if sid is not None:
            max_seq = max(max_seq, sid)

    # seq 单调：不小于任何实体序号
    if seq < max_seq:
        raise _bad(f"seq={seq} 小于已存在实体序号 {max_seq}（非单调）")

    # ---- 工单关闭守卫一致性 ----
    for tid, ticket in tickets.items():
        if ticket.status == TicketStatus.CLOSED:
            for oid, op in operations.items():
                if op.ticket_id == tid and op.status not in _TERMINAL_OPERATION:
                    raise _bad(f"工单 {tid} 已关闭但操作 {oid} 未终态（{op.status.value}）")

    # ---- 退款累计 == 已执行求和 且 ≤ 实付（refunded 出现即必须与执行一致） ----
    for oid in refunded:
        if oid not in orders:
            raise _bad(f"refunded 引用不存在的订单 {oid}")
        recorded = refunded[oid]
        _check_amount(f"refunded[{oid}]", recorded)
        total = executed_sum.get(oid, Decimal("0"))
        if recorded != total:
            raise _bad(f"订单 {oid} refunded={recorded} 与已执行求和 {total} 不一致")
        if total > orders[oid].paid_amount:
            raise _bad(f"订单 {oid} 退款累计 {total} 超过实付 {orders[oid].paid_amount}")

    # ---- 审计引用 ----
    for i, event in enumerate(audit):
        if not isinstance(event, AuditEvent):
            raise _bad(f"审计[{i}] 结构错误")
        if event.entity_type == "ticket" and event.entity_id not in tickets:
            raise _bad(f"审计[{i}] 引用不存在的工单 {event.entity_id}")
        if event.entity_type == "operation" and event.entity_id not in operations:
            raise _bad(f"审计[{i}] 引用不存在的操作 {event.entity_id}")

    # ---- 幂等记录：引用存在、无 pending 占位 ----
    known = set(tickets) | set(operations)
    for key, rec in idem.items():
        if not isinstance(rec, IdempotencyRecord):
            raise _bad(f"幂等记录 {key} 结构错误")
        if rec.refund_id == "<pending>":
            raise _bad(f"幂等记录 {key} 处于 pending 占位（快照不应包含进行中占位）")
        if rec.refund_id not in known:
            raise _bad(f"幂等记录 {key} 引用不存在的对象 {rec.refund_id}")
        if not rec.payload_hash:
            raise _bad(f"幂等记录 {key} payload_hash 为空")

    return True
