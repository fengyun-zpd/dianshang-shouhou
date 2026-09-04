"""统一 LLM Client 协议与模型安全守卫（阶段 5A）。

- LLMClient：所有模型实现（离线规则 / OpenAI-compatible / 未来本地模型）遵守的统一接口；
- 结构化输出：invoke 按 response_schema 校验，失败抛对应 ModelError 子类；
- 内容安全守卫：模型输出（文本或结构化字段）含金额/审批/状态/执行指令 → ModelContentPolicyError，
  调用方必须降级（离线规则或人工接管），绝不可把该输出接向写路径。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Type

from pydantic import BaseModel

from .schemas import ModelInvocationMetadata


# ---------- 异常层级（错误一律结构化，供网关安全降级） ----------

class ModelError(Exception):
    """模型调用基础错误。"""


class ModelConfigError(ModelError):
    """配置不满足安全要求（Key 缺失 / URL 不在白名单）。"""


class ModelQuotaExceededError(ModelError):
    """Token / 成本预算超限（未调用即拒绝）。"""


class ModelTimeoutError(ModelError):
    """调用超时。"""


class ModelRateLimitedError(ModelError):
    """限流（HTTP 429）。"""


class ModelHttpError(ModelError):
    """HTTP / 网络失败（5xx、断连等）。"""


class ModelNetworkError(ModelHttpError):
    """网络层失败（连接错误）。"""


class ModelParseError(ModelError):
    """返回内容不是合法 JSON / 无法解析。"""


class ModelSchemaError(ModelError):
    """返回 JSON 不满足输出 Schema。"""


class ModelContentPolicyError(ModelError):
    """输出违反内容安全策略（含金额 / 审批 / 状态 / 执行指令等）。"""


# ---------- 统一响应 ----------

@dataclass(frozen=True)
class ModelResponse:
    payload: BaseModel
    metadata: ModelInvocationMetadata


# ---------- 内容安全守卫（金额 / 审批 / 状态 / 执行指令检测） ----------

_MONEY_PATTERNS = (
    r"[¥￥]",
    r"\d+(?:\.\d+)?\s*(?:元|块|人民币|rmb|usd|美元)",
)
_ACTION_PATTERNS = (
    "批准", "审批通过", "同意退款", "执行退款", "退款已执行", "立即退款",
    "已批准", "已拒绝", "关闭工单", "修改地址", "状态已变更", "状态迁移",
)
_EN_ACTION_RE = re.compile(r"\b(approve|reject|execute|close\s+ticket|refund\s+now|change\s+address)\b", re.IGNORECASE)


def assert_output_safe(text: str) -> None:
    """文本内容安全守卫：命中金额/审批/状态/执行指令 → ModelContentPolicyError。"""
    if not text:
        return
    for pat in _ACTION_PATTERNS:
        if pat in text:
            raise ModelContentPolicyError(f"输出含禁止动作指令：{pat!r}")
    for pat in _MONEY_PATTERNS:
        if re.search(pat, text):
            raise ModelContentPolicyError(f"输出含金额内容（禁止模型产出金额）")
    if _EN_ACTION_RE.search(text):
        raise ModelContentPolicyError("输出含禁止的英文动作指令（approve/reject/execute/refund now…）")


def assert_payload_safe(payload: BaseModel) -> None:
    """递归检查结构化输出，避免嵌套 dict/list 藏匿动作或金额指令。"""
    def walk(value, seen: set[int]) -> None:
        if isinstance(value, str):
            assert_output_safe(value)
            return
        if isinstance(value, BaseModel):
            value = value.model_dump()
        if isinstance(value, dict):
            marker = id(value)
            if marker in seen:
                return
            seen.add(marker)
            for item in value.values():
                walk(item, seen)
            return
        if isinstance(value, (list, tuple, set)):
            marker = id(value)
            if marker in seen:
                return
            seen.add(marker)
            for item in value:
                walk(item, seen)

    walk(payload, set())


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（中英混合按字符/4 估算；仅用于预算记账）。"""
    return max(1, len(text) // 4 + 1)


# ---------- Client 协议 ----------

class LLMClient(ABC):
    """模型客户端统一协议。

    invoke(task, prompt, response_schema, inputs) → ModelResponse
    失败抛 ModelError 子类；调用方据此安全降级。input 中的敏感字段必须先脱敏。
    """

    provider: str = "abstract"
    model_name: str = "abstract"
    model_version: str = ""

    @abstractmethod
    def invoke(
        self,
        task: str,
        prompt: str,
        response_schema: Type[BaseModel],
        inputs: dict,
        dataset_version: str = "",
    ) -> ModelResponse:
        """执行一次模型调用并返回 Schema 校验后的结构化结果。"""
