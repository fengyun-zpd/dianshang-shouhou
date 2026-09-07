"""K5：API 层请求/响应 Schema（纯模型，不承载领域逻辑）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TicketCreateIn(BaseModel):
    order_id: str = Field(..., min_length=3)
    customer_id: str = Field(..., min_length=1)
    request_type: str = "refund"
    reason: str = Field(..., min_length=1)
    reason_tags: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(..., min_length=1)


class TicketOut(BaseModel):
    ticket_id: str
    tenant_id: str
    order_id: str
    customer_id: str
    request_type: str
    reason: str
    status: str
    resolution: Optional[str] = None


class RefundDraftIn(BaseModel):
    amount: str = Field(..., pattern=r"^\d+(\.\d{1,2})?$")
    reason_detail: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)


class OperationOut(BaseModel):
    operation_id: str
    ticket_id: str
    order_id: str
    op_type: str
    amount: Optional[str] = None
    status: str
    idempotency_key: Optional[str] = None
    version: int = 1
    decision_version: Optional[int] = None
    executed: bool = False


class DecisionIn(BaseModel):
    """审批/拒绝决定。expected_version：调用方已知的操作版本（期望）；不传时服务端以
    读到的当前版本提交（宽松兼容）；传入时与领域当前版本不符将返回 DECISION_VERSION_MISMATCH。"""
    reason: Optional[str] = None
    expected_version: Optional[int] = None


class ExecuteIn(BaseModel):
    external_result: str = Field(..., pattern="^(success|timeout)$")


class AgentLabTraceIn(BaseModel):
    mode: str = Field("single_agent", pattern="^(single_agent|multi_agent)$")


class AgentLabRetrieveIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=300)


class AgentLabBoundaryScenarioIn(BaseModel):
    name: str = Field(..., pattern="^(clarify|no_evidence|cross_tenant|pii)$")


class ReconcileIn(BaseModel):
    result: str = Field(..., pattern="^(success|failed)$")


class ErrorOut(BaseModel):
    request_id: str
    code: str
    message: str


class AuditItemOut(BaseModel):
    action: str
    entity_type: str
    entity_id: str
    actor: str
    before: Optional[str] = None
    after: Optional[str] = None
    note: Optional[str] = None
