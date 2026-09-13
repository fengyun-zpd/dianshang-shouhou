"""模型运行时安全配置读取（阶段 5A）+ 成本记账配置（阶段 5B）。

仅从环境变量读取（OPSPILOT_LLM_* 前缀），不做任何形式的密钥存储/日志输出。
安全约束：
- 未配置 OPSPILOT_LLM_API_KEY 时禁止联网（assert_safe_network 失败）；
- OPSPILOT_LLM_BASE_URL 必须在白名单内（前缀匹配；白名单可由
  OPSPILOT_LLM_ALLOWED_BASE_URLS 覆盖，默认含 OpenAI 官方与本地回环——便于本地测试）。

成本配置（不伪造成本）：
- 输入单价：`OPSPILOT_LLM_PRICE_PER_1K_INPUT`（新变量优先）→ 回退
  `OPSPILOT_LLM_COST_PER_1K_IN`（旧变量，仅输入单价）；
- 输出单价：`OPSPILOT_LLM_PRICE_PER_1K_OUTPUT`；
- 币种：`OPSPILOT_LLM_PRICE_CURRENCY`（默认 USD）；
- 任一单价缺失/非法 → 对应成本项为 None（显示 N/A），**绝不填 0 冒充真实成本**；
- 总成本仅在输入与输出单价**都**已配置时才计算，否则标记未配置。
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit
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

# ---------- 成本相关环境变量 ----------

PRICE_ENV_INPUT = "OPSPILOT_LLM_PRICE_PER_1K_INPUT"
PRICE_ENV_OUTPUT = "OPSPILOT_LLM_PRICE_PER_1K_OUTPUT"
PRICE_ENV_LEGACY_INPUT = "OPSPILOT_LLM_COST_PER_1K_IN"
PRICE_ENV_CURRENCY = "OPSPILOT_LLM_PRICE_CURRENCY"

PRICING_NOT_CONFIGURED = "未配置"
PRICING_SOURCE_LEGACY = f"{PRICE_ENV_LEGACY_INPUT}（旧变量：仅输入单价）"
PRICING_UNCONFIGURED_DISPLAY = "N/A（未配置价格）"


def _parse_price(raw: Optional[str]) -> tuple[Optional[float], bool]:
    """解析单价：返回 (价格, 是否非法)。

    - 未配置（None/空白）→ (None, False)；
    - 非法（非数字、负数、NaN、inf）→ (None, True)：**安全回退为未配置**，不猜测价格。
    """
    if raw is None or not str(raw).strip():
        return None, False
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None, True
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None, True
    return value, False


@dataclass(frozen=True)
class CostBreakdown:
    """一次模型调用的成本明细。None 表示该项未配置（显示 N/A），不是 0 成本。"""
    input_cost: Optional[float]
    output_cost: Optional[float]
    total_cost: Optional[float]
    currency: str
    pricing_source: str
    configured: bool                 # 输入与输出单价均已配置

    def display_total(self) -> str:
        if self.total_cost is None:
            return PRICING_UNCONFIGURED_DISPLAY
        return f"{self.currency} {self.total_cost:.6f}"

    def to_metadata_fields(self) -> dict:
        """投影到 ModelInvocationMetadata 的字段（只含数值与来源标记）。"""
        return {
            "input_cost": self.input_cost,
            "output_cost": self.output_cost,
            "cost_estimate_usd": self.total_cost,
            "currency": self.currency,
            "pricing_source": self.pricing_source,
        }


@dataclass(frozen=True)
class LLMSettings:
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: str = "gpt-4o-mini"
    model_version: str = "未配置"
    timeout_seconds: float = 10.0
    max_retries: int = 1
    token_budget_input: int = 4000          # 单次输入 token 预算
    cost_per_1k_input_usd: float = 0.0      # 【兼容保留】旧单价字段；成本计算改用下面两项
    input_price_per_1k: Optional[float] = None    # None = 未配置（成本 N/A）
    output_price_per_1k: Optional[float] = None   # None = 未配置（成本 N/A）
    currency: str = "USD"
    pricing_source: str = PRICING_NOT_CONFIGURED
    model_configured: bool = False          # 模型名是否由环境变量显式配置
    price_errors: tuple[str, ...] = ()      # 非法单价的变量名（安全回退为未配置）
    allowed_base_urls: tuple[str, ...] = DEFAULT_ALLOWED_BASE_URLS

    # ---------- 成本 ----------

    @property
    def pricing_configured(self) -> bool:
        """输入与输出单价都已配置（才可计算「总成本」）。"""
        return self.input_price_per_1k is not None and self.output_price_per_1k is not None

    def cost_breakdown(self, input_tokens: int, output_tokens: int) -> CostBreakdown:
        """按输入/输出 token 双向计算成本。

        input_cost  = input_tokens / 1000 * input_price
        output_cost = output_tokens / 1000 * output_price
        total_cost  = input_cost + output_cost（仅当两项单价都已配置）
        """
        in_tokens = max(0, int(input_tokens or 0))
        out_tokens = max(0, int(output_tokens or 0))

        input_cost = (None if self.input_price_per_1k is None
                      else round(in_tokens / 1000.0 * self.input_price_per_1k, 6))
        output_cost = (None if self.output_price_per_1k is None
                       else round(out_tokens / 1000.0 * self.output_price_per_1k, 6))
        total = (round(input_cost + output_cost, 6)
                 if input_cost is not None and output_cost is not None else None)
        return CostBreakdown(
            input_cost=input_cost, output_cost=output_cost, total_cost=total,
            currency=self.currency, pricing_source=self.pricing_source,
            configured=self.pricing_configured,
        )

    # ---------- 脱敏摘要 ----------

    def redacted_summary(self) -> str:
        """脱敏摘要（绝不含 api_key）。"""
        host = "未配置"
        if self.base_url:
            try:
                host = self.base_url.split("://", 1)[1].split("/", 1)[0]
            except IndexError:
                host = self.base_url
        return (f"base_url={self.base_url or '未配置'} (host={host}) model={self.model} "
                f"key={'已配置' if self.api_key else '未配置'} timeout={self.timeout_seconds}s "
                f"pricing={self.pricing_source}")


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

    # 成本：新变量优先；旧变量只提供输入单价
    price_errors: list[str] = []
    input_price, in_bad = _parse_price(env.get(PRICE_ENV_INPUT))
    output_price, out_bad = _parse_price(env.get(PRICE_ENV_OUTPUT))
    legacy_input, legacy_bad = _parse_price(env.get(PRICE_ENV_LEGACY_INPUT))
    if in_bad:
        price_errors.append(PRICE_ENV_INPUT)
    if out_bad:
        price_errors.append(PRICE_ENV_OUTPUT)
    if legacy_bad:
        price_errors.append(PRICE_ENV_LEGACY_INPUT)

    source_parts: list[str] = []
    if input_price is not None:
        source_parts.append(PRICE_ENV_INPUT)
    elif legacy_input is not None:
        input_price = legacy_input
        source_parts.append(PRICING_SOURCE_LEGACY)
    if output_price is not None:
        source_parts.append(PRICE_ENV_OUTPUT)
    pricing_source = " + ".join(source_parts) if source_parts else PRICING_NOT_CONFIGURED

    currency = (env.get(PRICE_ENV_CURRENCY) or "").strip().upper() or "USD"
    raw_model = (env.get("OPSPILOT_LLM_MODEL") or "").strip()

    return LLMSettings(
        api_key=env.get("OPSPILOT_LLM_API_KEY"),
        base_url=env.get("OPSPILOT_LLM_BASE_URL"),
        model=raw_model or "gpt-4o-mini",
        model_version=env.get("OPSPILOT_LLM_MODEL_VERSION", "未配置"),
        timeout_seconds=timeout,
        max_retries=retries,
        token_budget_input=budget,
        # 兼容旧字段：仅当输入单价来自旧变量或新变量时同步，未配置为 0.0（不参与成本计算）
        cost_per_1k_input_usd=input_price if input_price is not None else 0.0,
        input_price_per_1k=input_price,
        output_price_per_1k=output_price,
        currency=currency,
        pricing_source=pricing_source,
        model_configured=bool(raw_model),
        price_errors=tuple(price_errors),
        allowed_base_urls=allowed,
    )


def is_allowed_base_url(base_url: Optional[str], allowed: Sequence[str]) -> bool:
    """严格按 scheme/hostname/端口/路径边界校验 Base URL，拒绝 userinfo 与伪后缀域名。"""
    if not base_url:
        return False
    try:
        candidate = urlsplit(base_url)
        if candidate.scheme not in {"http", "https"} or not candidate.hostname:
            return False
        if candidate.username is not None or candidate.password is not None:
            return False
        for entry in allowed:
            rule = urlsplit(entry)
            if rule.scheme not in {"http", "https"} or not rule.hostname:
                continue
            if candidate.scheme != rule.scheme or candidate.hostname.lower() != rule.hostname.lower():
                continue
            # 远程白名单按协议默认端口归一化；回环地址可保留任意本地测试端口。
            if rule.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
                candidate_port = candidate.port or (443 if candidate.scheme == "https" else 80)
                rule_port = rule.port or (443 if rule.scheme == "https" else 80)
                if candidate_port != rule_port:
                    continue
            elif rule.port is not None and candidate.port != rule.port:
                continue
            rule_path = rule.path.rstrip("/")
            candidate_path = candidate.path.rstrip("/")
            if rule_path and not (candidate_path == rule_path or candidate_path.startswith(rule_path + "/")):
                continue
            if candidate.query or candidate.fragment:
                continue
            return True
    except ValueError:
        return False
    return False


def assert_safe_network(settings: LLMSettings) -> None:
    """联网前置安全校验：Key 缺失或 URL 不在白名单 → ModelConfigError（禁止偷偷联网）。

    这是唯一允许发起候选模型网络请求的前置条件；未通过时调用方必须安全降级离线。
    """
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
