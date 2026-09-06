"""Agent 运行模式开关单元测试（V1.1 阶段 3.6）。

覆盖：四值枚举、默认值（single_agent）、from_str 非法拒绝、
resolve_mode 在未配置 LLM Key / 白名单 Base URL 时把 llm 回落 offline_rule（不静默直连）。
"""
import pytest

from src.agents.modes import RuntimeMode, resolve_mode


def test_runtime_mode_values_and_default():
    assert {m.value for m in RuntimeMode} == {
        "single_agent", "multi_agent", "offline_rule", "llm"}
    assert RuntimeMode.default() is RuntimeMode.SINGLE_AGENT


@pytest.mark.parametrize("value,expected", [
    ("single_agent", RuntimeMode.SINGLE_AGENT),
    ("multi_agent", RuntimeMode.MULTI_AGENT),
    ("offline_rule", RuntimeMode.OFFLINE_RULE),
    ("llm", RuntimeMode.LLM),
])
def test_from_str_valid(value, expected):
    assert RuntimeMode.from_str(value) is expected


def test_from_str_rejects_unknown():
    with pytest.raises(ValueError):
        RuntimeMode.from_str("super-agent")


def test_resolve_llm_without_key_falls_back_offline(monkeypatch):
    """未配置安全 Key / 白名单 Base URL → llm 解析为 offline_rule（不静默直连）。"""
    monkeypatch.delenv("OPSPILOT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPSPILOT_LLM_BASE_URL", raising=False)
    assert resolve_mode("llm") is RuntimeMode.OFFLINE_RULE
    # 其它模式不受影响
    assert resolve_mode("single_agent") is RuntimeMode.SINGLE_AGENT
    assert resolve_mode("multi_agent") is RuntimeMode.MULTI_AGENT
    assert resolve_mode("offline_rule") is RuntimeMode.OFFLINE_RULE
