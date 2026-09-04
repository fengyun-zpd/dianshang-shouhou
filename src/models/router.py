"""模型路由与能力矩阵（阶段 5A）。

能力矩阵：
    intent_classification    售后意图识别            （低风险，默认启用）
    structured_extraction    缺参字段抽取            （低风险，默认启用）
    clarification_copy       用户澄清话术            （低风险，默认启用）
    evidence_explanation     基于已验证证据的方案解释（低风险，默认启用）
    tool_calling             模型选择工具            （高风险，默认禁用）
    high_risk_draft          高风险动作草稿          （高风险，默认禁用）

规则：
- 模型只做语言任务；即使矩阵放行，也只能产出解释/草稿文本，副作用必须走确定性领域服务；
- 高风险任务任何模型都不放行（raise ModelContentPolicyError）；
- ModelGateway 降级链：主模型（若配置且矩阵启用）→ 离线规则 → 失败即转人工信号（异常）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Type

from pydantic import BaseModel

from .base import (
    LLMClient,
    ModelConfigError,
    ModelContentPolicyError,
    ModelError,
    ModelResponse,
)
from .config import LLMSettings, load_llm_settings
from .offline import OfflineRuleClient
from .openai_compatible import OpenAICompatibleClient
from .prompts import PROMPT_VERSION, get_prompt
from .schemas import (
    ClarificationDecision,
    EvidenceBoundExplanation,
    IntentExtraction,
    ModelInvocationMetadata,
)


class ModelTask(str, Enum):
    intent_classification = "intent_classification"
    structured_extraction = "structured_extraction"
    clarification_copy = "clarification_copy"
    evidence_explanation = "evidence_explanation"
    tool_calling = "tool_calling"          # 高风险
    high_risk_draft = "high_risk_draft"    # 高风险

    @property
    def high_risk(self) -> bool:
        return self in HIGH_RISK_TASKS


HIGH_RISK_TASKS = frozenset({ModelTask.tool_calling, ModelTask.high_risk_draft})
LOW_RISK_TASKS = frozenset({
    ModelTask.intent_classification, ModelTask.structured_extraction,
    ModelTask.clarification_copy, ModelTask.evidence_explanation,
})

_SCHEMA_BY_TASK: dict[ModelTask, Type[BaseModel]] = {
    ModelTask.intent_classification: IntentExtraction,
    ModelTask.structured_extraction: IntentExtraction,
    ModelTask.clarification_copy: ClarificationDecision,
    ModelTask.evidence_explanation: EvidenceBoundExplanation,
}


@dataclass(frozen=True)
class ModelEntry:
    """注册表条目：客户端 + 能力矩阵。"""
    client: LLMClient
    capabilities: frozenset[ModelTask]

    def supports(self, task: ModelTask) -> bool:
        return task in self.capabilities


class ModelRegistry:
    """能力矩阵注册表：查询某任务允许的模型。"""

    def __init__(self) -> None:
        self._entries: dict[str, ModelEntry] = {}

    def register(self, entry: ModelEntry) -> None:
        self._entries[entry.client.model_name] = entry

    def model_names(self) -> list[str]:
        return list(self._entries)

    def supports(self, model_name: str, task: ModelTask) -> bool:
        entry = self._entries.get(model_name)
        return bool(entry and entry.supports(task))

    def allowed_models(self, task: ModelTask) -> list[str]:
        if task in HIGH_RISK_TASKS:
            return []  # 高风险默认禁用，即使矩阵里有也返回空
        return [name for name, e in self._entries.items() if e.supports(task)]


class ModelGateway:
    """面向业务调用的模型入口：统一路由 + 安全降级到离线规则。"""

    def __init__(
        self,
        offline: Optional[OfflineRuleClient] = None,
        registry: Optional[ModelRegistry] = None,
        settings: Optional[LLMSettings] = None,
        auto_load_settings: bool = False,
    ):
        self._offline = offline if offline is not None else OfflineRuleClient()
        self._registry = registry if registry is not None else ModelRegistry()
        self._settings = settings
        if settings is not None:
            self._primary = OpenAICompatibleClient(settings)  # 构造即做 Key/白名单安全校验
            self._registry.register(ModelEntry(
                client=self._primary, capabilities=LOW_RISK_TASKS,
            ))
        else:
            self._primary = None
            if auto_load_settings:
                self._attach_settings(load_llm_settings())

    def _attach_settings(self, settings: LLMSettings) -> None:
        """外部显式安全配置就绪时挂接主模型；配置不合法则保持仅离线（不抛到业务层）。"""
        self._settings = settings
        try:
            self._primary = OpenAICompatibleClient(settings)
            self._registry.register(ModelEntry(
                client=self._primary, capabilities=LOW_RISK_TASKS,
            ))
        except ModelConfigError:
            self._primary = None  # 安全失败：保持离线，不联网

    @property
    def has_primary_model(self) -> bool:
        return self._primary is not None

    # ---------- 领域便捷入口（低风险语言任务） ----------

    def analyze_intent(self, text: str, order_id_hint: Optional[str] = None,
                       dataset_version: str = "") -> ModelResponse:
        return self.run_task(ModelTask.intent_classification, text,
                             inputs={"text": text, "order_id_hint": order_id_hint},
                             dataset_version=dataset_version)

    def decide_clarification(self, text: str, missing_fields: list[str],
                             dataset_version: str = "") -> ModelResponse:
        return self.run_task(
            ModelTask.clarification_copy, text,
            inputs={"text": text, "missing_fields": missing_fields},
            dataset_version=dataset_version,
        )

    def explain_with_evidence(self, text: str, allowed_evidence: list[str],
                              dataset_version: str = "") -> ModelResponse:
        return self.run_task(
            ModelTask.evidence_explanation, text,
            inputs={"text": text, "allowed_evidence": allowed_evidence},
            dataset_version=dataset_version,
        )

    # ---------- 统一路由 + 降级 ----------

    def run_task(self, task: ModelTask, text: str, inputs: dict,
                 dataset_version: str = "") -> ModelResponse:
        if task.high_risk:
            raise ModelContentPolicyError(
                f"能力矩阵：高风险任务 {task.value} 默认禁用，模型不得参与",
            )
        schema = _SCHEMA_BY_TASK.get(task)
        if schema is None:
            raise ModelContentPolicyError(f"任务 {task.value} 无输出 Schema")
        prompt = get_prompt(task.value)

        # 1) 主模型路径（矩阵启用且存在）
        if self._primary is not None and self._registry.supports(self._primary.model_name, task):
            try:
                return self._primary.invoke(task.value, prompt, schema, inputs, dataset_version)
            except ModelError as e:
                reason = type(e).__name__
                # 2) 安全降级到离线规则
                off = self._offline.invoke(task.value, prompt, schema, inputs, dataset_version)
                meta = off.metadata.model_copy(
                    update={"degraded": True, "error": reason,
                            "provider": f"{off.metadata.provider}",
                            "model_name": off.metadata.model_name},
                )
                return ModelResponse(payload=off.payload, metadata=meta)

        # 3) 无主模型（离线模式）→ 直接离线（非降级）
        return self._offline.invoke(task.value, prompt, schema, inputs, dataset_version)
