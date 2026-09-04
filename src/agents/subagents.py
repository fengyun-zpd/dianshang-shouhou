"""受控只读子 Agent（阶段 5B，ADR-002 多 Agent 实验）。

边界（宪法第三/四条）：
- 子 Agent 只有**只读工具白名单**，注册表内不允许出现任何写工具；
- 子 Agent 调用仍携带 TenantContext 并经 ToolRegistry 角色/租户/Schema/超时/审计校验；
- 所有写命令只存在于顶层 Supervisor（领域服务 + 人工审批），子 Agent 无任何写路径。

Supervisor 证据编排：将原单 Agent 的 gather_evidence 替换为并行调用
order-agent / history-agent / policy-agent 的节点，输出与单 Agent 相同的
order_summary / evidence_refs 语义（状态 Schema 不变，便于 A/B 对照）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from src.platform.tooling import TenantContext, ToolRegistry, ToolResult

from .ports import AfterSalesGateway
from .state import AgentState
from .toolkit import build_toolkit
from src.rag import PolicyStore
from src.domain.models import Role


@dataclass(frozen=True)
class SubAgentSpec:
    """子 Agent 声明：职责 + 允许调用的只读工具白名单。"""
    name: str
    role: str
    allowed_tools: tuple[str, ...]


ORDER_AGENT_SPEC = SubAgentSpec(
    name="order-agent", role="订单核验与客户上下文", allowed_tools=("get_order",),
)
HISTORY_AGENT_SPEC = SubAgentSpec(
    name="history-agent", role="历史工单证据", allowed_tools=("list_customer_tickets",),
)
POLICY_AGENT_SPEC = SubAgentSpec(
    name="policy-agent", role="政策证据检索（引用）", allowed_tools=("retrieve_policy",),
)


class ReadOnlySubAgent:
    """包一个只读工具白名单的受控子 Agent。"""

    def __init__(self, spec: SubAgentSpec, registry: ToolRegistry):
        self.spec = spec
        available = {t["name"]: t for t in registry.list_tools()}
        for tool in spec.allowed_tools:
            if tool not in available:
                raise ValueError(f"子 Agent {spec.name}：工具 {tool} 未注册")
            if not available[tool]["read_only"]:
                raise ValueError(f"子 Agent {spec.name}：工具 {tool} 不是只读，禁止白名单化")
        self._registry = registry

    @property
    def tool_names(self) -> list[str]:
        return list(self.spec.allowed_tools)

    def call(self, ctx: TenantContext, tool: str, args: dict) -> ToolResult:
        """只允许白名单工具；越界即 PERMISSION_DENIED。"""
        if tool not in self.spec.allowed_tools:
            return ToolResult.error("PERMISSION_DENIED",
                                    f"子 Agent {self.spec.name} 无权调用工具 {tool}")
        return self._registry.invoke(ctx, tool, args)


def build_supervisor_evidence(
    gateway: AfterSalesGateway,
    policy_store: Optional[PolicyStore],
    max_workers: int = 2,
):
    """构造 Supervisor 的 gather_evidence 替换节点（并行子 Agent 证据编排）。

    若 policy_store 为空：policy-agent 不可用（不虚构证据），证据只含订单/历史/物流。
    """
    registry = build_toolkit(gateway, policy_store) if policy_store is not None else build_toolkit(gateway, PolicyStore())
    order_agent = ReadOnlySubAgent(ORDER_AGENT_SPEC, registry)
    history_agent = ReadOnlySubAgent(HISTORY_AGENT_SPEC, registry)
    policy_agent = None
    if policy_store is not None:
        policy_agent = ReadOnlySubAgent(POLICY_AGENT_SPEC, registry)

    def supervise_evidence(state: AgentState) -> dict:
        tenant = state.get("tenant_id")
        order_id = state.get("order_id")
        if not tenant or not order_id:
            return {"error_code": "MISSING_ORDER_ID", "outcome": "escalated",
                    "next_action": "escalate", "reply": "缺少订单号，无法核验（应已澄清）"}
        ctx = TenantContext(tenant_id=tenant, actor=Role.AGENT,
                            trace_id=f"super-{state.get('thread_id', 'x')}")

        # 政策检索以用户原始请求为准（含自然语言关键词），标签作为补充
        user_request = state.get("user_request") or ""
        policy_query = (user_request.strip() or " ".join(state.get("reason_tags") or [])) + " 售后政策"

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            f_order = pool.submit(order_agent.call, ctx, "get_order", {"order_id": order_id})
            f_policy = None
            if policy_agent is not None:
                f_policy = pool.submit(policy_agent.call, ctx, "retrieve_policy",
                                       {"query": policy_query, "top_k": 3})
            order_result = f_order.result()

        if not order_result.ok:
            # 域错误原样透传（订单不存在 / 跨租户）→ 转人工（与单 Agent 语义一致）
            return {"error_code": order_result.error_code, "outcome": "escalated",
                    "next_action": "escalate",
                    "reply": f"订单核验未通过（{order_result.detail}），已转人工，不继续生成草稿"}
        order = order_result.data

        # 历史工单（依赖订单客户，串行）
        history_result = history_agent.call(ctx, "list_customer_tickets",
                                            {"customer_id": order["customer_id"]})
        history_ids = history_result.data["ticket_ids"] if history_result.ok else []

        # 政策证据（可选；失败/无证据不阻断，领域资格判定为准）
        policy_citations: list[str] = []
        policy_notes: list[str] = []
        if f_policy is not None:
            policy_result = f_policy.result()
            if policy_result.ok:
                policy_citations = [r["citation"] for r in policy_result.data["results"]]
            else:
                policy_notes.append(policy_result.error_code or "POLICY_UNAVAILABLE")

        logistics = gateway.logistics_status(tenant, order_id)
        refs = [f"order:{order['order_id']}", f"customer:{order['customer_id']}"]
        refs += [f"history:{t}" for t in history_ids]
        refs += [f"policy:{c}" for c in policy_citations]
        refs.append("logistics:unavailable")
        return {
            "evidence_refs": refs,
            "order_summary": {
                "order_id": order["order_id"],
                "customer_id": order["customer_id"],
                "status": order["status"],
                "paid_amount": order["paid_amount"],
                "logistics": logistics,
                "history_count": len(history_ids),
                "policy_citations": policy_citations,
                "supervisor": {
                    "agents": ["order-agent", "history-agent"] + (["policy-agent"] if policy_agent else []),
                    "policy_notes": policy_notes,
                },
            },
        }

    return supervise_evidence
