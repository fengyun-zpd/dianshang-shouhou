"""Mule Agent Bridge 数据模型（阶段 6 / ADR-003）。

桥接层只做协议/身份/租户适配；业务裁决仍在本地领域服务。
动作白名单：只读查询 + 发起售后请求（内部走客服入口语义，AGENT 角色草稿）；
**绝不暴露**审批、执行、状态放行或任何写领域放行能力。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from src.domain.models import Role


class BridgeAction(str, Enum):
    """外部 Agent 网络可调用的动作白名单。"""
    query_order = "query_order"
    query_ticket = "query_ticket"
    list_customer_tickets = "list_customer_tickets"
    retrieve_policy = "retrieve_policy"
    submit_after_sales_request = "submit_after_sales_request"  # 发起内部售后流程（不含审批权）


# 明确禁止出现在白名单的高风险动作（测试将断言不可达）
FORBIDDEN_ACTIONS = ("approve", "reject", "execute", "close_ticket", "change_address",
                     "high_risk_draft", "refund_now")


@dataclass(frozen=True)
class BridgeIdentity:
    """身份映射：外部 principal → 本地租户/角色/可调动作。"""
    external_principal: str
    tenant_id: str
    local_role: Role
    allowed_actions: frozenset[BridgeAction]
    label: str = ""


class IdentityRegistry:
    """外部身份注册表（V1 内存实现；生产可换配置/DB）。"""

    def __init__(self) -> None:
        self._map: dict[str, BridgeIdentity] = {}

    def register(self, identity: BridgeIdentity) -> None:
        self._map[identity.external_principal] = identity

    def resolve(self, principal: str) -> Optional[BridgeIdentity]:
        return self._map.get(principal)

    def is_registered(self, principal: str) -> bool:
        return principal in self._map


# ---------- 入站 Schema（严格校验） ----------

class QueryOrderPayload(BaseModel):
    order_id: str = Field(..., min_length=3)


class QueryTicketPayload(BaseModel):
    ticket_id: str = Field(..., min_length=3)


class ListTicketsPayload(BaseModel):
    customer_id: str = Field(..., min_length=1)


class RetrievePolicyPayload(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(3, ge=1, le=10)


class SubmitRequestPayload(BaseModel):
    request_text: str = Field(..., min_length=1)
    thread_hint: Optional[str] = None


INBOUND_SCHEMAS: dict[BridgeAction, type[BaseModel]] = {
    BridgeAction.query_order: QueryOrderPayload,
    BridgeAction.query_ticket: QueryTicketPayload,
    BridgeAction.list_customer_tickets: ListTicketsPayload,
    BridgeAction.retrieve_policy: RetrievePolicyPayload,
    BridgeAction.submit_after_sales_request: SubmitRequestPayload,
}


# ---------- 出站 Schema（每动作结构化结果） ----------

class OrderView(BaseModel):
    order_id: str
    tenant_id: str
    customer_id: str
    status: str
    paid_amount: str
    days_since_sign: int


class TicketView(BaseModel):
    ticket_id: str
    tenant_id: str
    order_id: str
    customer_id: str
    request_type: str
    status: str
    resolution: Optional[str] = None


class TicketList(BaseModel):
    customer_id: str
    count: int
    ticket_ids: list[str]


class PolicyEvidenceView(BaseModel):
    query: str
    has_evidence: bool
    results: list[dict] = []


class RequestReceipt(BaseModel):
    thread_id: str
    waiting_clarify: bool = False
    waiting_approval: bool = False
    ticket_id: Optional[str] = None
    operation_id: Optional[str] = None
    outcome: Optional[str] = None
    message: str = ""


OUTBOUND_SCHEMAS: dict[BridgeAction, type[BaseModel]] = {
    BridgeAction.query_order: OrderView,
    BridgeAction.query_ticket: TicketView,
    BridgeAction.list_customer_tickets: TicketList,
    BridgeAction.retrieve_policy: PolicyEvidenceView,
    BridgeAction.submit_after_sales_request: RequestReceipt,
}


class BridgeEnvelope(BaseModel):
    """统一出站封装（data 已按 OUTBOUND_SCHEMAS 校验）。"""
    action: BridgeAction
    ok: bool
    data: dict = {}
    error_code: Optional[str] = None
    error_detail: str = ""


@dataclass
class BridgeLogEntry:
    """桥接审计（不含 PII / 密钥 / 请求体原文）。"""
    ts: float = field(default_factory=time.time)
    principal: str = ""
    action: str = ""
    tenant_id: str = ""
    status: str = ""          # ok / error:<code>
    duration_ms: float = 0.0
    detail: str = ""          # 仅错误码等短信息
