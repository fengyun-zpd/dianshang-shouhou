"""离线规则适配器（阶段 5A）：与现有规则基线一致的 LLMClient 实现。

正常业务默认走离线规则；候选模型影子评测以本实现为对照基线。
本适配器不联网、不产生副作用，输出与 src.agents.intent 规则完全一致。
"""
from __future__ import annotations

import time

from src.agents.intent import clarify_questions, extract_intent

from .base import LLMClient, ModelResponse, ModelSchemaError, estimate_tokens
from .schemas import (
    ClarificationDecision,
    EvidenceBoundExplanation,
    IntentExtraction,
    IntentKind,
    ModelInvocationMetadata,
)


class OfflineRuleClient(LLMClient):
    """规则实现（离线）；可作为任何任务的回退基线。"""

    provider = "offline-rule"
    model_name = "offline/rules-v1"
    model_version = "1.0"

    def invoke(self, task, prompt, response_schema, inputs, dataset_version=""):
        started = time.monotonic()
        text = inputs.get("text", "") or ""
        order_id_hint = inputs.get("order_id_hint")

        if response_schema is IntentExtraction:
            res = extract_intent(text, order_id_hint)
            intent = IntentKind(res["intent"]) if res["intent"] in IntentKind._value2member_map_ else IntentKind.unknown
            payload = IntentExtraction(
                intent=intent,
                order_id=res["order_id"],
                reason_tags=res["reason_tags"],
                missing_fields=res["missing_fields"],
                confidence=1.0,
                note="offline 规则基线",
            )
        elif response_schema is ClarificationDecision:
            missing = list(inputs.get("missing_fields") or [])
            payload = ClarificationDecision(
                should_clarify=bool(missing),
                missing_fields=missing,
                questions=clarify_questions(missing) if missing else [],
                intent_hint=IntentExtraction(
                    **extract_intent(text, order_id_hint)
                ).intent if text else None,
            )
        elif response_schema is EvidenceBoundExplanation:
            allowed = [str(e) for e in (inputs.get("allowed_evidence") or [])]
            payload = EvidenceBoundExplanation(
                supported=bool(allowed),
                evidence_refs=allowed[:5],
                explanation=(
                    f"离线规则基线：已依据 {len(allowed)} 条已验证政策证据说明处理方向；"
                    "具体金额、资格与审批由确定性系统裁决。"
                ),
                boundaries=["不产出金额", "不做出审批决定", "不迁移状态"],
            )
        else:
            raise ModelSchemaError(f"offline 适配器不支持输出 Schema：{response_schema.__name__}")

        duration = (time.monotonic() - started) * 1000.0
        meta = ModelInvocationMetadata(
            provider=self.provider, model_name=self.model_name,
            model_version=self.model_version, task=task,
            prompt_version="N/A（规则）", dataset_version=dataset_version,
            duration_ms=round(duration, 2),
            input_tokens=estimate_tokens(text), output_tokens=0, cost_estimate_usd=0.0,
            degraded=False,
        )
        return ModelResponse(payload=payload, metadata=meta)
