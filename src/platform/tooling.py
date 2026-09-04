"""受控工具契约层（阶段 3）：严格 JSON Schema 工具、TenantContext、权限拦截、超时、审计与输出校验。

对齐 AGENTS.md 第三、四条：
- 默认只读工具；写工具必须强类型并经由确定性领域服务（本层不放开）；
- TenantContext 强绑定租户：工具入参不允许携带其他租户 id，跨租户尝试一律拒绝；
- 每次调用记录审计（ToolCallRecord），供追溯；输出必须通过出参 Schema 校验；
- 超时返回 TOOL_TIMEOUT（只读工具可放弃等待；写工具禁止超时后盲目重试，必须走幂等对账）；
- MCP 为跨服务协议（规划，ADR-003），本层是进程内工具注册/调用实现。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Type

from pydantic import BaseModel, ValidationError

from src.domain.models import Role

ToolExecutor = Callable[["TenantContext", BaseModel], dict]


class ToolCallError(Exception):
    """工具调用结构化错误。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class TenantContext:
    """工具调用上下文：租户与角色强绑定，trace/request id 用于审计关联。"""
    tenant_id: str
    actor: Role
    trace_id: Optional[str] = None
    request_id: Optional[str] = None


@dataclass(frozen=True)
class ToolSpec:
    """工具契约：入参/出参均用 pydantic 模型（可导出严格 JSON Schema）。"""
    name: str
    description: str
    input_schema: Type[BaseModel]
    output_schema: Type[BaseModel]
    executor: ToolExecutor
    read_only: bool = True
    allowed_actors: tuple[Role, ...] = (Role.AGENT, Role.APPROVER, Role.SYSTEM)
    timeout_ms: int = 3000

    def input_json_schema(self) -> dict:
        return self.input_schema.model_json_schema()

    def output_json_schema(self) -> dict:
        return self.output_schema.model_json_schema()


@dataclass
class ToolCallRecord:
    """一次工具调用的审计记录（输出内容只存脱敏摘要）。"""
    tool: str
    actor: str
    tenant_id: str
    status: str            # ok | error:<code>
    duration_ms: float
    args_summary: str
    detail: str = ""
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ToolResult:
    """工具调用结果（错误不抛异常，结构化返回给编排层处理）。"""
    ok: bool
    data: Optional[dict] = None
    error_code: Optional[str] = None
    detail: str = ""

    @classmethod
    def success(cls, data: dict) -> "ToolResult":
        return cls(ok=True, data=data)

    @classmethod
    def error(cls, code: str, detail: str = "") -> "ToolResult":
        return cls(ok=False, error_code=code, detail=detail)


# 只读工具默认超时与线程池（进程内执行器；写工具需经领域服务，不允许此处在超时后重试）
_EXECUTOR = ThreadPoolExecutor(max_workers=4)


class ToolRegistry:
    """工具注册表与受控执行器。

    调用链路：查工具 → 角色权限 → 防跨租户参数 → 入参 Schema 校验 → 执行(超时)
            → 出参 Schema 校验 → 审计记录。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._calls: list[ToolCallRecord] = []

    # ---------- 注册 ----------

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具 {spec.name} 已注册")
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def list_tools(self) -> list[dict]:
        return [{"name": t.name, "description": t.description,
                 "read_only": t.read_only,
                 "input_schema": t.input_json_schema()} for t in self._tools.values()]

    def call_trace(self) -> list[ToolCallRecord]:
        return list(self._calls)

    # ---------- 调用 ----------

    def invoke(self, ctx: TenantContext, name: str, args: dict) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult.error("UNKNOWN_TOOL", f"未注册工具：{name}")

        # 1) 角色权限
        if ctx.actor not in spec.allowed_actors:
            return ToolResult.error(
                "PERMISSION_DENIED", f"角色 {ctx.actor.value} 无权调用 {name}",
            )

        # 2) 防跨租户：显式 tenant_id 必须与上下文一致（剥离后继续）；不一致直接拒绝
        args = dict(args)
        explicit_tenant = args.pop("tenant_id", None)
        if explicit_tenant is not None and str(explicit_tenant) != ctx.tenant_id:
            self._record(spec, ctx, "error:PERMISSION_DENIED", args,
                         detail="显式 tenant_id 与上下文不一致（疑似跨租户）")
            return ToolResult.error("PERMISSION_DENIED", "tenant_id 与上下文不一致")

        # 3) 入参 Schema 校验
        try:
            model = spec.input_schema.model_validate(args)
        except ValidationError as e:
            self._record(spec, ctx, "error:VALIDATION_ERROR", args, detail=str(e)[:500])
            return ToolResult.error("VALIDATION_ERROR", f"入参校验失败：{e.errors()[:3]}")

        # 4) 执行（带超时）
        started = time.monotonic()
        try:
            future = _EXECUTOR.submit(spec.executor, ctx, model)
            data = future.result(timeout=spec.timeout_ms / 1000.0)
        except FutureTimeout:
            self._record(spec, ctx, "error:TIMEOUT", args)
            return ToolResult.error("TOOL_TIMEOUT", f"工具 {name} 执行超过 {spec.timeout_ms}ms")
        except ToolCallError as e:
            self._record(spec, ctx, f"error:{e.code}", args, detail=e.message[:500])
            return ToolResult.error(e.code, e.message)
        except Exception as e:  # noqa: BLE001
            self._record(spec, ctx, "error:EXECUTION_ERROR", args, detail=repr(e)[:500])
            return ToolResult.error("EXECUTION_ERROR", f"执行异常：{e!r}")
        duration = (time.monotonic() - started) * 1000.0

        # 5) 出参 Schema 校验（工具返回内容视为不可信数据）
        try:
            spec.output_schema.model_validate(data)
        except ValidationError as e:
            self._record(spec, ctx, "error:OUTPUT_VALIDATION_ERROR", args,
                         detail=f"输出未通过 Schema：{e.errors()[:3]}", duration=duration)
            return ToolResult.error("OUTPUT_VALIDATION_ERROR", "工具输出未通过 Schema 校验")

        self._record(spec, ctx, "ok", args, duration=duration)
        return ToolResult.success(data)

    # ---------- 内部 ----------

    def _record(self, spec: ToolSpec, ctx: TenantContext, status: str,
                args: dict, detail: str = "", duration: float = 0.0) -> None:
        self._calls.append(ToolCallRecord(
            tool=spec.name, actor=ctx.actor.value, tenant_id=ctx.tenant_id,
            status=status, duration_ms=round(duration, 2),
            args_summary=_summarize(args), detail=detail,
        ))


def _summarize(args: dict, limit: int = 200) -> str:
    """参数摘要（不含值细节，仅键与长度，避免审计污染）。"""
    return ", ".join(f"{k}=<{len(str(v))} chars>" for k, v in sorted(args.items()))[:limit]
