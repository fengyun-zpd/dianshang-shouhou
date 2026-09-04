"""模型安全配置测试：Base URL 白名单、Key 缺失、脱敏摘要。"""
import pytest

from src.models import ModelConfigError
from src.models.config import (
    DEFAULT_ALLOWED_BASE_URLS,
    LLMSettings,
    assert_safe_network,
    is_allowed_base_url,
    load_llm_settings,
)


def test_is_allowed_base_url_prefix_match():
    assert is_allowed_base_url("https://api.openai.com/v1", DEFAULT_ALLOWED_BASE_URLS)
    assert is_allowed_base_url("https://api.openai.com:443/v1", DEFAULT_ALLOWED_BASE_URLS)
    assert is_allowed_base_url("http://127.0.0.1:8000/v1", DEFAULT_ALLOWED_BASE_URLS)
    assert not is_allowed_base_url("https://evil.example.com", DEFAULT_ALLOWED_BASE_URLS)
    assert not is_allowed_base_url(None, DEFAULT_ALLOWED_BASE_URLS)
    assert not is_allowed_base_url("", DEFAULT_ALLOWED_BASE_URLS)


def test_base_url_rejects_domain_and_userinfo_bypass():
    for url in (
        "https://api.openai.com.evil.com/v1",
        "https://api.openai.com@evil.com/v1",
        "http://localhost.evil.com/v1",
        "https://api.openai.com:8443/v1",
    ):
        assert not is_allowed_base_url(url, DEFAULT_ALLOWED_BASE_URLS)


def test_assert_safe_network_missing_key():
    s = LLMSettings(api_key=None, base_url="https://api.openai.com/v1")
    with pytest.raises(ModelConfigError):
        assert_safe_network(s)


def test_assert_safe_network_url_not_allowed():
    s = LLMSettings(api_key="sk-test", base_url="https://evil.example.com/v1")
    with pytest.raises(ModelConfigError):
        assert_safe_network(s)


def test_assert_safe_network_ok():
    s = LLMSettings(api_key="sk-test", base_url="https://api.openai.com/v1")
    assert_safe_network(s)  # 不抛


def test_load_settings_from_env_allowlist_override():
    env = {
        "OPSPILOT_LLM_API_KEY": "sk-x",
        "OPSPILOT_LLM_BASE_URL": "https://my-proxy.example.com/v1",
        "OPSPILOT_LLM_ALLOWED_BASE_URLS": "https://my-proxy.example.com",
        "OPSPILOT_LLM_TIMEOUT_S": "3.5",
    }
    s = load_llm_settings(env)
    assert s.api_key == "sk-x"
    assert s.timeout_seconds == 3.5
    assert is_allowed_base_url("https://my-proxy.example.com/v1", s.allowed_base_urls)


def test_redacted_summary_never_contains_key():
    s = LLMSettings(api_key="sk-super-secret", base_url="https://api.openai.com/v1")
    summary = s.redacted_summary()
    assert "sk-super-secret" not in summary
    assert "api.openai.com" in summary
