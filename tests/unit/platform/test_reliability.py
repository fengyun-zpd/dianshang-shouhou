"""平台可靠性测试：有限重试 / 熔断 / 只读降级 / 人工接管标记。"""
import pytest

from src.domain.models import Role
from src.platform import reliability
from src.platform.reliability import (
    AuditMarkers,
    CircuitBreaker,
    CircuitOpenError,
    RetriesExhausted,
    RetryPolicy,
    TakeoverMarker,
    retry_call,
)


class _Flaky:
    def __init__(self, fail_times: int, error_type: type = TimeoutError):
        self.fail_times = fail_times
        self.error_type = error_type
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error_type("temporary")
        return "ok"


# ---------- 有限重试 ----------

def test_retry_succeeds_after_transient_failures():
    flaky = _Flaky(fail_times=2)
    assert retry_call(flaky, RetryPolicy(max_attempts=3)) == "ok"
    assert flaky.calls == 3


def test_retry_does_not_catch_business_errors():
    def boom():
        raise ValueError("业务错误不可重试")
    with pytest.raises(ValueError):
        retry_call(boom, RetryPolicy(max_attempts=3))


def test_retry_exhausted_raises():
    flaky = _Flaky(fail_times=99)
    with pytest.raises(RetriesExhausted):
        retry_call(flaky, RetryPolicy(max_attempts=2))


# ---------- 熔断 ----------

def test_breaker_opens_after_threshold_then_fallback():
    cb = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=60)
    def fail():
        raise TimeoutError("boom")
    for _ in range(3):
        with pytest.raises(TimeoutError):
            cb.call(fail)
    assert cb.state == "open"
    # 熔断打开 → 走只读降级 fallback（不触碰写路径）
    assert cb.call(fail, fallback=lambda: "readonly-degraded") == "readonly-degraded"


def test_breaker_fail_closed_without_fallback():
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=60)
    def fail():
        raise TimeoutError("boom")
    with pytest.raises(TimeoutError):
        cb.call(fail)
    with pytest.raises(CircuitOpenError):
        cb.call(fail)  # 无降级 → fail-closed


def test_breaker_half_open_recovers_after_reset_window(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(reliability.time, "monotonic", lambda: now[0])
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=1.0)
    def fail():
        raise TimeoutError("boom")
    def ok():
        return "ok"
    with pytest.raises(TimeoutError):
        cb.call(fail)
    assert cb.state == "open"
    now[0] += 1.0           # 虚拟时钟越过重置窗口，避免依赖真实调度时间
    assert cb.state == "half_open"
    assert cb.call(ok) == "ok"   # 试探成功 → closed
    assert cb.state == "closed"


def test_breaker_reset():
    cb = CircuitBreaker(failure_threshold=1)
    def fail():
        raise TimeoutError("boom")
    with pytest.raises(TimeoutError):
        cb.call(fail)
    cb.reset()
    assert cb.state == "closed"


# ---------- 人工接管标记 ----------

def test_takeover_markers():
    m = AuditMarkers()
    m.record(TakeoverMarker(subject_id="TKT-1", kind="takeover",
                            reason="证据不足转人工", by=Role.AGENT))
    m.record(TakeoverMarker(subject_id="TKT-2", kind="degrade_readonly",
                            reason="工具超时降级", by=Role.SYSTEM))
    assert m.count() == 2
    assert m.count("takeover") == 1
    assert m.count("degrade_readonly") == 1
