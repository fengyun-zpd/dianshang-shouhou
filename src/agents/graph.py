"""LangGraph 单 Agent 工作流图组装（阶段 2，任务卡 B）。

运行时说明：
- checkpointer（默认 MemorySaver）只保存流程恢复状态（checkpoint），不保存业务真相；
- 业务真相（工单/操作/审批/执行/审计）只存在于确定性领域服务；
- 图中唯一的两处 interrupt：clarify（缺参澄清）与 approval（人工审批）。
"""
from __future__ import annotations

from typing import Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .nodes import (
    build_nodes,
    route_after_decision,
    route_after_draft,
    route_after_gather,
    route_after_parse,
    route_after_plan,
)
from .ports import AfterSalesGateway
from .state import AgentState


def build_workflow(gateway: AfterSalesGateway, checkpointer=None):
    """构建并编译单 Agent 售后工作流。

    节点路径：
    parse →(缺参)→ clarify ⇄ parse
    parse →(无法识别/不支持动作)→ escalate
    parse →(refund)→ gather_evidence → plan → create_ticket_draft
    → request_approval(interrupt) → apply_decision(重读领域决定)
      → execute_operation / settle_rejected / finish（幂等收尾）
    escalate → END；execute/settle/escalate → END
    """
    nodes = build_nodes(gateway)
    g = StateGraph(AgentState)
    for name, fn in nodes.items():
        g.add_node(name, fn)

    g.add_edge(START, "parse")
    g.add_conditional_edges("parse", route_after_parse, {
        "clarify": "clarify",
        "escalate": "escalate",
        "gather_evidence": "gather_evidence",
    })
    g.add_edge("clarify", "parse")  # 补参后回到意图识别/缺参判断

    g.add_conditional_edges("gather_evidence", route_after_gather, {
        "plan": "plan",
        "escalate": "escalate",
    })
    g.add_conditional_edges("plan", route_after_plan, {
        "create_ticket_draft": "create_ticket_draft",
        "escalate": "escalate",
    })

    # 草稿落库后：正常进入人工审批；重复请求已终态则直接收尾（不重复审批/执行）
    g.add_conditional_edges("create_ticket_draft", route_after_draft, {
        "request_approval": "request_approval",
        "end": END,
    })
    g.add_edge("request_approval", "apply_decision")
    g.add_conditional_edges("apply_decision", route_after_decision, {
        "execute_operation": "execute_operation",
        "settle_rejected": "settle_rejected",
        "escalate": "escalate",
        "finished": END,
        "reconcile_required": END,
    })

    for name in ("execute_operation", "settle_rejected", "escalate"):
        g.add_edge(name, END)

    return g.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())
