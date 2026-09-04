"""模型运行时安全配置读取（阶段 5A）。

仅从环境变量读取（OPSPILOT_LLM_* 前缀），不做任何形式的密钥存储/日志输出。
安全约束：
- 未配置 OPSPILOT_LLM_API_KEY 时禁止联网（assert_safe_network 失败）；
- OPSPILOT_LLM_BASE_URL 必须在白名单内（前缀匹配；白名单可由
  OPSPILOT_LLM_ALLOWED_BASE_URLS 覆盖，默认含 OpenAI 官方与本地回环——便于本地测试）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .base import ModelConfigError

DEFAULT_ALLOWED_BASE_URLS: tuple[str, ...] = (
    "https://api.openai.com",
    "https://api.moonshot.cn",
    "https://api.deepseek.com",
    "https://dashscope.aliyuncs.com",
    "http://127.0.0.1",   # 本地 mock/测试端点
    "http://localhost",
)


@dataclass(frozen=True)
class LLMSettings:
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: str = "gpt-4o-mini"
    model_version: str = "未配置"
    timeout_seconds: float = 10.0
    max_retries: int = 1
    token_budget_input: int = 4000          # 单次输入 token 预算
    cost_per_1k_input_usd: float = 0.0      # 单价（未配置按 0 记账并标注 N/A）
    allowed_base_urls: tuple[str, ...] = DEFAULT_ALLOWED_BASE_URLS

    def redacted_summary(self) -> str:
        """脱敏摘要（绝不含 api_key）。"""
        host = "未配置"
        if self.base_url:
            try:
                host = self.base_url.split("://", 1)[1].split("/", 1)[0]
            except IndexError:
                host = self.base_url
        return (f"base_url={self.base_url or '未配置'} (host={host}) model={self.model} "
                f"key={'已配置' if self.api_key else '未配置'} timeout={self.timeout_seconds}s")


def load_llm_settings(env=None) -> LLMSettings:
    """从环境变量读取配置；未设置项为 None/默认。"""
    env = env if env is not None else os.environ

    allowed = DEFAULT_ALLOWED_BASE_URLS
    extra = (env.get("OPSPILOT_LLM_ALLOWED_BASE_URLS") or "").strip()
    if extra:
        allowed = tuple(a.strip() for a in extra.split(",") if a.strip())

    try:
        timeout = float(env.get("OPSPILOT_LLM_TIMEOUT_S", "10.0"))
    except ValueError:
        timeout = 10.0
    try:
        retries = int(env.get("OPSPILOT_LLM_MAX_RETRIES", "1"))
    except ValueError:
        retries = 1
    try:
        budget = int(env.get("OPSPILOT_LLM_TOKEN_BUDGET_IN", "4000"))
    except ValueError:
        budget = 4000
    try:
        cost = float(env.get("OPSPILOT_LLM_COST_PER_1K_IN", "0"))
    except ValueError:
        cost = 0.0

    return LLMSettings(
        api_key=env.get("OPSPILOT_LLM_API_KEY"),
        base_url=env.get("OPSPILOT_LLM_BASE_URL"),
        model=env.get("OPSPILOT_LLM_MODEL", "gpt-4o-mini"),
        model_version=env.get("OPSPILOT_LLM_MODEL_VERSION", "未配置"),
        timeout_seconds=timeout,
        max_retries=retries,
        token_budget_input=budget,
        cost_per_1k_input_usd=cost,
        allowed_base_urls=allowed,
    )


def is_allowed_base_url(base_url: Optional[str], allowed: Sequence[str]) -> bool:
    """Base URL 白名单校验（前缀匹配，防止指向非白名单端点）。"""
    if not base_url:
        return False
    for prefix in allowed:
        if base_url.startswith(prefix):
            return True
    return False


def assert_safe_network(settings: LLMSettings) -> None:
    """联网前置安全校验：Key 缺失或 URL 不在白名单 → ModelConfigError（禁止偷偷联网）。"""
    if not settings.api_key or not str(settings.api_key).strip():
        raise ModelConfigError(
            "未配置 OPSPILOT_LLM_API_KEY：模型候选模式已安全禁用（不会联网）。"
            "可先以 --mode offline 运行基线。"
        )
    if not is_allowed_base_url(settings.base_url, settings.allowed_base_urls):
        raise ModelConfigError(
            f"Base URL {settings.base_url!r} 不在白名单内，拒绝联网。"
            f"允许前缀：{list(settings.allowed_base_urls)}",
        )
