"""K5：API 层请求/响应 Schema（纯模型，不承载领域逻辑）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


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


class AgentStartIn(BaseModel):
    """Agent 启动请求（V1 只服务内部坐席；客户入口属规划能力）。

    安全边界（extra="forbid"）：请求体**只**接受用户诉求与线程标识。
    出现 tenant_id / 金额 / 角色 / 审批结果 / 外部执行结果等字段 → 422（拒绝而非静默忽略）；
    租户只由认证身份推导；外部执行结果只能由 SYSTEM 角色的领域执行接口写入。
    """
    model_config = {"extra": "forbid"}

    message: str = Field(..., min_length=1, max_length=2000)
    thread_id: Optional[str] = Field(None, min_length=1, max_length=100)
    order_id_hint: Optional[str] = Field(None, min_length=3, max_length=64)


class AgentClarifyIn(BaseModel):
    """澄清补参请求。

    安全边界（extra="forbid"）：只允许补「信息」，不允许借澄清接口修改租户、金额、
    审批状态、操作状态或权限；至少提供一个有效字段（空载荷不会推进工作流）。
    """
    model_config = {"extra": "forbid"}

    message: Optional[str] = Field(None, min_length=1, max_length=2000)
    order_id: Optional[str] = Field(None, min_length=3, max_length=64)
    description: Optional[str] = Field(None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if not any((self.message, self.order_id, self.description)):
            raise ValueError("澄清请求至少需要 message / order_id / description 之一（空载荷不推进工作流）")
        return self


class AgentDecisionIn(BaseModel):
    """决策触发请求：允许空 body，且**不接受**任何审批结论字段。

    审批结果必须先经领域审批接口写入事实源（/api/operations/{id}/approve|reject）；
    本接口只触发 WorkflowRunner.resume，apply_decision 会重新读取带版本的审批事实。
    携带 approved/rejected/decision 等字段 → 422（拒绝伪造审批结论）。
    """
    model_config = {"extra": "forbid"}


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
