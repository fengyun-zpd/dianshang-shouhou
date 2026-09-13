"""Agent 工具调用去重账本（确定性保护，不依赖模型自觉停止或去重）。

职责边界（AGENTS.md 第三、四、五条）：
- 去重键 = (工具名, 租户, thread_id, 参数摘要)，只作用于**受控写工具**；
- 同一线程同一工具同一参数重复调用时复用首次结果，**不发起第二次领域调用**；
- 本账本是第二道防线，**不替代**领域服务幂等键：任何重复副作用最终仍由领域幂等兜底；
- 审批事实重读（`get_operation`）**永不进入账本**——审批决定必须每次读取领域事实源的
  最新版本，缓存会破坏「审批以领域事实为准」这一硬边界；
- 账本只保存同进程内的返回值引用与参数**摘要**，不写 checkpoint、不写数据库、
  不记录参数原文（避免 PII 落入内存账本或日志）。
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

# 参与去重记账的工具（受控写）。
DEDUPED_TOOLS: frozenset[str] = frozenset({
    "create_ticket",
    "create_refund",
    "submit",
    "close_ticket",
    "execute",
    "reconcile",
})

# 明确豁免的工具：事实重读必须穿透到领域事实源（禁止缓存）。
NEVER_DEDUPED_TOOLS: frozenset[str] = frozenset({
    "get_operation",       # 审批/操作状态重读（apply_decision 硬边界）
    "query_operation",     # operation_unknown 原键查询
})


@dataclass(frozen=True)
class ToolCallContext:
    """当前节点执行上下文：提供去重键所需的租户与线程（不进入 checkpoint）。"""
    tenant_id: Optional[str]
    thread_id: str
    node: str


_current_context: ContextVar[Optional[ToolCallContext]] = ContextVar(
    "opspilot_tool_call_context", default=None)


@contextmanager
def tool_call_context(tenant_id: Optional[str], thread_id: Optional[str],
                      node: str) -> Iterator[None]:
    """在节点执行期间绑定 (tenant, thread, node)，供网关构造去重键。"""
    token = _current_context.set(ToolCallContext(tenant_id, thread_id or "", node))
    try:
        yield
    finally:
        _current_context.reset(token)


def current_tool_context() -> Optional[ToolCallContext]:
    return _current_context.get()


def _stable(value: Any) -> Any:
    """把参数值规范化为可稳定序列化的形式（领域对象只取类型名，不展开内容）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_stable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _stable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    return f"<{type(value).__name__}>"


def args_digest(args: dict) -> str:
    """参数摘要：规范化 JSON 的 sha256 前 16 位（单向，不保存参数原文）。"""
    payload = json.dumps(_stable(args), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class ToolCallRecord:
    tool: str
    tenant_id: str
    thread_id: str
    args_digest: str
    result: Any = None
    reused: int = 0


class ToolCallLedger:
    """同进程工具调用去重账本（键空间 = 工具 × 租户 × 线程 × 参数摘要）。"""

    def __init__(self, max_entries: int = 512):
        self._records: dict[tuple[str, str, str, str], ToolCallRecord] = {}
        self._order: list[tuple[str, str, str, str]] = []
        self._max_entries = max(1, int(max_entries))
        self.tool_calls = 0
        self.dedup_hits = 0

    @staticmethod
    def key(tool: str, tenant_id: Optional[str], thread_id: Optional[str],
            args: dict) -> tuple[str, str, str, str]:
        return (tool, tenant_id or "", thread_id or "", args_digest(args))

    def lookup(self, key: tuple[str, str, str, str]) -> Optional[ToolCallRecord]:
        return self._records.get(key)

    def remember(self, key: tuple[str, str, str, str], result: Any) -> None:
        if key in self._records:
            return
        self._records[key] = ToolCallRecord(*key, result=result)
        self._order.append(key)
        while len(self._order) > self._max_entries:
            stale = self._order.pop(0)
            self._records.pop(stale, None)

    def invoke(self, tool: str, tenant_id: Optional[str], thread_id: Optional[str],
               args: dict, fn: Callable[[], Any]) -> Any:
        """执行一次工具调用；命中重复键则复用首次结果（不调用 fn）。"""
        self.tool_calls += 1
        key = self.key(tool, tenant_id, thread_id, args)
        hit = self._records.get(key)
        if hit is not None:
            self.dedup_hits += 1
            hit.reused += 1
            return hit.result
        result = fn()
        self.remember(key, result)
        return result

    def stats(self) -> dict:
        """只含计数，不含任何参数原文或返回值。"""
        return {
            "tool_calls": self.tool_calls,
            "dedup_hits": self.dedup_hits,
            "entries": len(self._records),
            "deduped_tools": sorted(DEDUPED_TOOLS),
            "never_deduped_tools": sorted(NEVER_DEDUPED_TOOLS),
        }

    def clear(self) -> None:
        self._records.clear()
        self._order.clear()
        self.tool_calls = 0
        self.dedup_hits = 0
