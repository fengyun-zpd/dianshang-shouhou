"""领域状态 ↔ JSON 安全编解码（可恢复持久化原型）。

通用递归编解码：dataclass 实体、Enum、Decimal、tuple、Optional 均安全转换；
枚举/实体按名称注册（本仓库内类型名唯一）。
"""
from __future__ import annotations

import dataclasses
import enum
from dataclasses import is_dataclass
from decimal import Decimal
from typing import Any

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
from src.domain.idempotency import IdempotencyRecord
from src.domain.models import Role

_ENUM_BY_NAME = {
    cls.__name__: cls for cls in (
        OrderStatus, RequestType, TicketStatus, OperationStatus, OperationType, Role,
    )
}
_DC_BY_NAME = {
    cls.__name__: cls for cls in (
        OrderItem, Order, PolicyRule, AfterSalesTicket, Operation, AuditEvent,
        IdempotencyRecord,
    )
}


def _encode(value: Any) -> Any:
    if value is None:
        return None
    # 注意顺序：str-mixin 枚举（str, Enum）必须优先于 str 基元判断
    if isinstance(value, enum.Enum):
        return {"@e": value.__class__.__name__, "v": value.value}
    if isinstance(value, Decimal):
        return {"@d": str(value)}
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, tuple):
        return {"@t": [_encode(x) for x in value]}
    if isinstance(value, list):
        return [_encode(x) for x in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if is_dataclass(value):
        return {"@dc": value.__class__.__name__,
                **{f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    raise TypeError(f"不可序列化类型：{type(value)}")


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if "@d" in value:
            return Decimal(value["@d"])
        if "@e" in value:
            return _ENUM_BY_NAME[value["@e"]](value["v"])
        if "@t" in value:
            return tuple(_decode(x) for x in value["@t"])
        if "@dc" in value:
            cls = _DC_BY_NAME[value["@dc"]]
            kwargs = {k: _decode(v) for k, v in value.items() if k != "@dc"}
            return cls(**kwargs)
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(x) for x in value]
    return value


def state_to_jsonable(state: dict) -> dict:
    """export_state() 输出 → 可 JSON 化 dict。"""
    return _encode(state)


def state_from_jsonable(jsonable: dict) -> dict:
    """JSON 化快照 → 与 export_state() 同形的 dict（可直接 restore_state）。"""
    return _decode(jsonable)


class SnapshotCodecError(ValueError):
    """快照编解码失败。"""
