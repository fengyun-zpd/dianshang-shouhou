"""模型结构化输入/输出模型（阶段 5A）。

模型输出必须经这些 Pydantic Schema 校验；任何不匹配 → ModelSchemaError → 降级。
内容安全：字段只承载“语言任务”结果（意图/缺参/澄清问题/解释），
金额、审批决定、状态迁移一律不在此数据面出现；守卫层另行拦截文本注入。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class IntentKind(str, Enum):
    refund = "refund"
    return_ = "return"
    exchange = "exchange"
    replace = "replace"
    upgrade = "upgrade"
    other = "other"
    unknown = "unknown"


class IntentExtraction(BaseModel):
    """售后意图识别 + 缺参字段抽取（低风险语言任务）。"""
    intent: IntentKind
    order_id: Optional[str] = None
    reason_tags: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    note: str = ""  # 仅允许中性说明；含金额/审批/状态指令将被内容守卫拒绝


class ClarificationDecision(BaseModel):
    """是否需要缺参澄清、澄清问题与话术（模板可生成）。"""
    should_clarify: bool
    missing_fields: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    tone: str = "neutral"
    intent_hint: Optional[IntentKind] = None


class EvidenceBoundExplanation(BaseModel):
    """基于已验证证据的方案解释（证据引用必须来自调用方提供的白名单）。"""
    supported: bool
    evidence_refs: list[str] = Field(default_factory=list)
    explanation: str = ""
    boundaries: list[str] = Field(default_factory=list)  # 系统边界说明（金额/审批由系统决定等）


class ModelInvocationMetadata(BaseModel):
    """单次模型调用元数据（评测与记账）。未实测字段留空/N/A，不填虚构值。"""
    provider: str
    model_name: str
    model_version: str = ""
    task: str
    prompt_version: str
    dataset_version: str = ""
    duration_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_estimate_usd: float = 0.0
    degraded: bool = False          # 是否降级（True=未走主模型）
    error: Optional[str] = None     # 降级原因（如 "TIMEOUT"/"SCHEMA_ERROR"）
