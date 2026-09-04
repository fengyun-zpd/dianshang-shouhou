"""平台可靠性模块（阶段 4）：有限重试、熔断、只读降级与人工接管标记。

对齐仓储 docs/06_reliability 与 ADR-006：
- fail-closed：不确定一律不自动放行写副作用；
- 有限重试只用于幂等/可重试操作；写操作超时/未知必须走 operation_unknown（领域层），
  禁止在本层“换键重试”；
- 熔断打开时优先只读降级，否则转人工（不猜测）；
- 每次降级 / 接管都留审计痕迹（marker）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, TypeVar

from src.domain.models import Role

T = TypeVar("T")


class CircuitOpenError(Exception):
    """熔断器打开：调用被快速失败拦截。"""


class RetriesExhausted(Exception):
    """有限重试耗尽。"""


# ---------- 有限重试 ----------

@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 0.005


def is_retryable(exc: Exception) -> bool:
    """可重试错误判定（默认：超时/瞬时 IO；业务领域错误不可重试）。"""
    name = type(exc).__name__.lower()
    return "timeout" in name or "temporary" in name or "connection" in name


def retry_call(
    fn: Callable[[], T],
    policy: RetryPolicy = RetryPolicy(),
    retry_if: Callable[[Exception], bool] = is_retryable,
) -> T:
    """有限重试调用；耗尽抛 RetriesExhausted。业务错误（不可重试）立即上抛。"""
    last: Optional[Exception] = None
    for attempt in range(policy.max_attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if not retry_if(e):
                raise
            if attempt + 1 < policy.max_attempts:
                time.sleep(policy.backoff_seconds)
    assert last is not None
    raise RetriesExhausted(f"{policy.max_attempts} 次重试后仍失败：{last!r}") from last


# ---------- 熔断 ----------

@dataclass
class CircuitBreaker:
    """简单熔断器（closed → open → half_open）。失败达到阈值打开；重置窗口后放行试探。"""
    failure_threshold: int = 3
    reset_timeout_seconds: float = 1.0

    _state: str = field(default="closed", init=False)   # closed | open | half_open
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: Optional[float] = field(default=None, init=False)

    @property
    def state(self) -> str:
        if self._state == "open" and self._opened_at is not None \
                and time.monotonic() - self._opened_at >= self.reset_timeout_seconds:
            self._state = "half_open"
        return self._state

    def call(self, fn: Callable[[], T], fallback: Optional[Callable[[], T]] = None) -> T:
        """执行调用；熔断打开时走 fallback（只读降级），无 fallback 抛 CircuitOpenError。"""
        st = self.state
        if st == "open":
            if fallback is not None:
                return fallback()
            raise CircuitOpenError("熔断打开且无降级路径（fail-closed）")
        try:
            result = fn()
        except Exception:  # noqa: BLE001
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = time.monotonic()
            raise
        self._consecutive_failures = 0
        self._state = "closed"
        return result

    def reset(self) -> None:
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at = None


# ---------- 只读降级 / 人工接管标记 ----------

@dataclass(frozen=True)
class TakeoverMarker:
    """人工接管 / 降级审计痕迹。"""
    subject_id: str            # ticket_id / thread_id
    kind: str                  # takeover | degrade_readonly
    reason: str
    by: Role
    ts: float = field(default_factory=time.time)

    def summary(self) -> str:
        return f"[{self.kind}] {self.subject_id} by {self.by.value}: {self.reason}"


class AuditMarkers:
    """人工接管与降级事件记录（追加式，供审计与评测报告）。"""

    def __init__(self) -> None:
        self._markers: list[TakeoverMarker] = []

    def record(self, marker: TakeoverMarker) -> None:
        self._markers.append(marker)

    def list(self) -> list[TakeoverMarker]:
        return list(self._markers)

    def count(self, kind: Optional[str] = None) -> int:
        if kind is None:
            return len(self._markers)
        return sum(1 for m in self._markers if m.kind == kind)
