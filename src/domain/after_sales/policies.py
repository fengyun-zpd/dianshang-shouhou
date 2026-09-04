"""售后政策证据（V1 结构化规则，非 RAG）。

政策以版本化规则对象表达；匹配与冲突检测为纯函数，确定性可测。
冲突政策（多条适用规则给出不同退款比例）必须显式暴露，由服务层转人工，
不得由 Agent 自行"选一条"。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence, Tuple

from .models import RequestType


@dataclass(frozen=True)
class PolicyRule:
    """一条适用政策规则。

    - window_days：签收后有效天数；days_since_sign > window_days 不适用；
    - refund_ratio：适用时允许的退款比例（1.0 = 全额），冲突由不同比例体现；
    - 版本号供未来政策版本化审计（V1 默认 1）。
    """
    policy_id: str
    tenant_id: str
    request_type: RequestType
    reason_tags: Tuple[str, ...]
    window_days: int
    refund_ratio: Decimal
    version: int = 1

    def __post_init__(self) -> None:
        if not (Decimal("0.00") <= self.refund_ratio <= Decimal("1.00")):
            raise ValueError(f"refund_ratio 必须在 [0,1]，收到 {self.refund_ratio!r}")


def match_policies(
    policies: Sequence[PolicyRule],
    tenant_id: str,
    request_type: RequestType,
    reason_tags: Tuple[str, ...],
    days_since_sign: int,
) -> list[PolicyRule]:
    """返回适用政策：租户匹配 + 诉求类型匹配 + 诉求标签命中 + 窗口期内。"""
    tags = set(reason_tags)
    matched: list[PolicyRule] = []
    for p in policies:
        if p.tenant_id != tenant_id:
            continue
        if p.request_type != request_type:
            continue
        if not (tags & set(p.reason_tags)):
            continue
        if days_since_sign > p.window_days:
            continue
        matched.append(p)
    return matched


def detect_conflict(matched: Sequence[PolicyRule]) -> Optional[PolicyRule]:
    """冲突判定：适用政策给出不一致的退款比例 → 返回引发冲突的规则。

    无冲突返回 None。V1 定义：>1 条适用且比例不一致即冲突（转人工）。
    """
    if len(matched) < 2:
        return None
    first_ratio = matched[0].refund_ratio
    for p in matched[1:]:
        if p.refund_ratio != first_ratio:
            return p
    return None
