"""业务只读工具集（阶段 3）：把确定性领域只读查询与 RAG 政策检索暴露为受控工具。

- 每个工具入参模型不含 tenant_id：租户一律取自 TenantContext（强绑定，防跨租户）；
- 全部只读；写操作不走工具层（直接经网关 → 领域服务）；
- 领域错误码原样透出（不被改写成成功）；RAG 无证据 → NO_EVIDENCE（上层转人工）；
- 检索查询命中注入模式 → INJECTION_DETECTED（不返回文档内容）。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from src.domain.after_sales import AfterSalesError
from src.platform.tooling import (
    TenantContext,
    ToolCallError,
    ToolRegistry,
    ToolSpec,
)
from src.rag.store import InjectionDetected, PolicyStore

from .ports import AfterSalesGateway


# ---------- 入/出参模型（严格 JSON Schema） ----------

class GetOrderInput(BaseModel):
    order_id: str = Field(..., min_length=3, description="订单号")


class GetOrderOutput(BaseModel):
    order_id: str
    tenant_id: str
    customer_id: str
    status: str
    paid_amount: str
    days_since_sign: int


class GetTicketInput(BaseModel):
    ticket_id: str = Field(..., min_length=3)


class GetTicketOutput(BaseModel):
    ticket_id: str
    tenant_id: str
    order_id: str
    customer_id: str
    request_type: str
    reason: str
    status: str
    resolution: Optional[str] = None


class ListTicketsInput(BaseModel):
    customer_id: str = Field(..., min_length=1)


class ListTicketsOutput(BaseModel):
    customer_id: str
    count: int
    ticket_ids: list[str]


class RetrievePolicyInput(BaseModel):
    query: str = Field(..., min_length=1, description="政策问题（如：破损如何退款）")
    top_k: int = Field(3, ge=1, le=10)


class EvidenceItem(BaseModel):
    citation: str
    policy_id: str
    text: str
    score: float
    keyword_hits: list[str] = []


class RetrievePolicyOutput(BaseModel):
    query: str
    has_evidence: bool
    results: list[EvidenceItem] = []


# ---------- 工具构造 ----------

def _raise_domain(err: AfterSalesError) -> None:
    raise ToolCallError(err.code.value, err.message)


def build_toolkit(gateway: AfterSalesGateway, store: PolicyStore,
                  timeout_ms: int = 3000) -> ToolRegistry:
    reg = ToolRegistry()

    def _get_order(ctx: TenantContext, m: GetOrderInput) -> dict:
        try:
            order = gateway.get_order(ctx.tenant_id, m.order_id)
        except AfterSalesError as e:
            _raise_domain(e)
        return {
            "order_id": order.order_id, "tenant_id": order.tenant_id,
            "customer_id": order.customer_id, "status": order.status.value,
            "paid_amount": str(order.paid_amount), "days_since_sign": order.days_since_sign,
        }

    reg.register(ToolSpec(
        name="get_order",
        description="只读查询订单（租户内）",
        input_schema=GetOrderInput, output_schema=GetOrderOutput,
        executor=_get_order, timeout_ms=timeout_ms,
    ))

    def _get_ticket(ctx: TenantContext, m: GetTicketInput) -> dict:
        try:
            ticket = gateway.get_ticket_for(ctx.tenant_id, m.ticket_id)
        except AfterSalesError as e:
            _raise_domain(e)
        return {
            "ticket_id": ticket.ticket_id, "tenant_id": ticket.tenant_id,
            "order_id": ticket.order_id, "customer_id": ticket.customer_id,
            "request_type": ticket.request_type.value, "reason": ticket.reason,
            "status": ticket.status.value, "resolution": ticket.resolution,
        }

    reg.register(ToolSpec(
        name="get_ticket",
        description="只读查询售后工单（租户内）",
        input_schema=GetTicketInput, output_schema=GetTicketOutput,
        executor=_get_ticket, timeout_ms=timeout_ms,
    ))

    def _list_tickets(ctx: TenantContext, m: ListTicketsInput) -> dict:
        tickets = gateway.history_tickets(ctx.tenant_id, m.customer_id)
        return {
            "customer_id": m.customer_id,
            "count": len(tickets),
            "ticket_ids": [t.ticket_id for t in tickets],
        }

    reg.register(ToolSpec(
        name="list_customer_tickets",
        description="只读查询客户历史工单（租户内）",
        input_schema=ListTicketsInput, output_schema=ListTicketsOutput,
        executor=_list_tickets, timeout_ms=timeout_ms,
    ))

    def _retrieve_policy(ctx: TenantContext, m: RetrievePolicyInput) -> dict:
        try:
            evs = store.search(ctx.tenant_id, m.query, top_k=m.top_k)
        except InjectionDetected as e:
            raise ToolCallError("INJECTION_DETECTED", f"{e}") from e
        if not evs:
            raise ToolCallError(
                "NO_EVIDENCE", "未检索到适用政策证据，请转人工核对（不猜测政策）",
            )
        return {
            "query": m.query,
            "has_evidence": True,
            "results": [
                {
                    "citation": ev.citation(),
                    "policy_id": ev.chunk.policy_id,
                    "text": ev.chunk.text,
                    "score": ev.score,
                    "keyword_hits": ev.keyword_hits,
                }
                for ev in evs
            ],
        }

    reg.register(ToolSpec(
        name="retrieve_policy",
        description="检索适用售后政策证据（租户内，返回带引用分块）",
        input_schema=RetrievePolicyInput, output_schema=RetrievePolicyOutput,
        executor=_retrieve_policy, timeout_ms=timeout_ms,
    ))

    return reg
