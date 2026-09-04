"""受控工具契约层测试：Schema、权限、校验、超时、审计与防跨租户。"""
import time

import pytest
from pydantic import BaseModel, Field

from src.domain.models import Role
from src.platform.tooling import (
    TenantContext,
    ToolCallError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class EchoInput(BaseModel):
    value: int = Field(..., ge=0)


class EchoOutput(BaseModel):
    doubled: int


class SlowInput(BaseModel):
    value: int = 1


class SlowOutput(BaseModel):
    ok: bool = True


def _echo(ctx: TenantContext, m: EchoInput) -> dict:
    return {"doubled": m.value * 2}


def _bad_output(ctx: TenantContext, m: EchoInput) -> dict:
    return {"doubled": "not-an-int"}  # 输出不符合 Schema


def _slow(ctx: TenantContext, m: SlowInput) -> dict:
    time.sleep(0.5)
    return {"ok": True}


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="echo", description="echo", input_schema=EchoInput,
        output_schema=EchoOutput, executor=_echo, timeout_ms=3000,
    ))
    reg.register(ToolSpec(
        name="bad_output", description="bad", input_schema=EchoInput,
        output_schema=EchoOutput, executor=_bad_output, timeout_ms=3000,
    ))
    reg.register(ToolSpec(
        name="slow", description="slow", input_schema=SlowInput,
        output_schema=SlowOutput, executor=_slow, timeout_ms=50,
    ))
    return reg


def _ctx(tenant="T1", actor=Role.AGENT) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor=actor, trace_id="tr-1")


def test_schema_generation():
    reg = _make_registry()
    spec = reg.get("echo")
    js = spec.input_json_schema()
    assert js["properties"]["value"]["type"] == "integer"
    assert "minLength" not in js  # int 无 minLength
    tools = reg.list_tools()
    assert {t["name"] for t in tools} == {"echo", "bad_output", "slow"}


def test_invoke_success_and_trace():
    reg = _make_registry()
    r = reg.invoke(_ctx(), "echo", {"value": 21})
    assert r.ok and r.data == {"doubled": 42}
    trace = reg.call_trace()
    assert trace[-1].status == "ok"
    assert trace[-1].tool == "echo" and trace[-1].actor == "agent"


def test_unknown_tool():
    reg = _make_registry()
    r = reg.invoke(_ctx(), "nope", {})
    assert not r.ok and r.error_code == "UNKNOWN_TOOL"


def test_role_permission_denied():
    reg = _make_registry()
    r = reg.invoke(_ctx(actor=Role.CUSTOMER), "echo", {"value": 1})
    assert not r.ok and r.error_code == "PERMISSION_DENIED"


def test_input_validation_error():
    reg = _make_registry()
    r = reg.invoke(_ctx(), "echo", {"value": "abc"})
    assert not r.ok and r.error_code == "VALIDATION_ERROR"


def test_output_validation_error():
    reg = _make_registry()
    r = reg.invoke(_ctx(), "bad_output", {"value": 1})
    assert not r.ok and r.error_code == "OUTPUT_VALIDATION_ERROR"


def test_timeout_returns_tool_timeout():
    reg = _make_registry()
    r = reg.invoke(_ctx(), "slow", {"value": 1})
    assert not r.ok and r.error_code == "TOOL_TIMEOUT"


def test_explicit_tenant_id_mismatch_rejected():
    reg = _make_registry()
    r = reg.invoke(_ctx(tenant="T1"), "echo", {"value": 1, "tenant_id": "T2"})
    assert not r.ok and r.error_code == "PERMISSION_DENIED"


def test_explicit_tenant_id_matching_stripped():
    reg = _make_registry()
    r = reg.invoke(_ctx(tenant="T1"), "echo", {"value": 1, "tenant_id": "T1"})
    assert r.ok  # 与上下文一致 → 容忍并剥离


def test_register_duplicate_rejected():
    reg = _make_registry()
    with pytest.raises(ValueError):
        reg.register(ToolSpec(
            name="echo", description="dup", input_schema=EchoInput,
            output_schema=EchoOutput, executor=_echo,
        ))
