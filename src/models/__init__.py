"""受控 LLM 运行时（可替换 / 可评测 / 可安全降级）。

模型只承担低风险语言任务；金额、资格、审批、状态与写操作永远由确定性领域服务裁决。

**V1.1 标注（如实）：本包是「未接入 V1.1 主链路的离线安全基线」**——真实 LLM 未实测
（无安全 Key / 不允许 Base URL），仅提供离线规则适配器（src/models/offline.py）与
影子评测入口（evals/run_model_shadow_eval.py --mode offline，意图准确率基于合成
黄金集）；没有任何真实模型指标，不代表真实模型效果。V1.1 主链路（单 Agent 工作流、
确定性领域服务、PG profile、最小 RAG）不依赖本包。
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
