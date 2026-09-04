"""阶段 5A：受控 LLM 运行时（可替换 / 可评测 / 可安全降级）。

模型只承担低风险语言任务；金额、资格、审批、状态与写操作永远由确定性领域服务裁决。
"""
from .base import (
    LLMClient,
    ModelConfigError,
    ModelContentPolicyError,
    ModelError,
    ModelHttpError,
    ModelNetworkError,
    ModelParseError,
    ModelQuotaExceededError,
    ModelRateLimitedError,
    ModelResponse,
    ModelSchemaError,
    ModelTimeoutError,
    assert_output_safe,
    assert_payload_safe,
    estimate_tokens,
)
from .config import LLMSettings, assert_safe_network, load_llm_settings
from .offline import OfflineRuleClient
from .openai_compatible import OpenAICompatibleClient
from .router import HIGH_RISK_TASKS, LOW_RISK_TASKS, ModelEntry, ModelGateway, ModelRegistry, ModelTask
from .schemas import (
    ClarificationDecision,
    EvidenceBoundExplanation,
    IntentExtraction,
    IntentKind,
    ModelInvocationMetadata,
)

__all__ = [
    "ClarificationDecision",
    "EvidenceBoundExplanation",
    "HIGH_RISK_TASKS",
    "IntentExtraction",
    "IntentKind",
    "LLMClient",
    "LLMSettings",
    "LOW_RISK_TASKS",
    "ModelConfigError",
    "ModelContentPolicyError",
    "ModelEntry",
    "ModelError",
    "ModelGateway",
    "ModelHttpError",
    "ModelInvocationMetadata",
    "ModelNetworkError",
    "ModelParseError",
    "ModelQuotaExceededError",
    "ModelRateLimitedError",
    "ModelRegistry",
    "ModelResponse",
    "ModelSchemaError",
    "ModelTask",
    "ModelTimeoutError",
    "OfflineRuleClient",
    "OpenAICompatibleClient",
    "assert_output_safe",
    "assert_payload_safe",
    "assert_safe_network",
    "estimate_tokens",
    "load_llm_settings",
]
