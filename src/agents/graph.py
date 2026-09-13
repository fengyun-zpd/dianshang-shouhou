"""LangGraph 单 Agent 工作流图组装（阶段 2，任务卡 B）。

运行时说明：
- checkpointer（默认 MemorySaver）只保存流程恢复状态（checkpoint），不保存业务真相；
- 业务真相（工单/操作/审批/执行/审计）只存在于确定性领域服务；
- 图中唯一的两处 interrupt：clarify（缺参澄清）与 approval（人工审批）。

死循环保护（确定性，不依赖模型自觉）：
1. 节点包装器：每个节点执行前把状态里的 `step_count` 加一；超过 `max_steps`（默认 32）
   → 抛 `AgentLoopDetected`，**在调用任何领域写操作之前**安全停止；
2. LangGraph recursion limit：`cfg["recursion_limit"]` 作为第二道防线，兜住「单次 invoke
   内部的超级步循环」，触发 `GraphRecursionError`，由运行器统一映射为 AGENT_LOOP_DETECTED。

两类保护都不改写领域错误码、不伪造成功、不产生任何副作用。
"""
from __future__ import annotations

import logging
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
from .tool_ledger import tool_call_context

LOOP_ERROR_CODE = "AGENT_LOOP_DETECTED"
DEFAULT_MAX_STEPS = 32

logger = logging.getLogger("opspilot.agent")


class AgentLoopDetected(RuntimeError):
    """工作流超过节点步数上限，必须安全停止（不继续调用任何领域写操作）。"""


def build_workflow(gateway: AfterSalesGateway, checkpointer=None,
                   evidence_node=None, mode: str = "single",
                   policy_store=None, max_steps: int = DEFAULT_MAX_STEPS):
    """构建并编译售后工作流。

    - mode="single"（默认）：单 Agent 闭环；
    - mode="supervisor"：evidence_node 为 Supervisor 并行子 Agent 编排节点时，
      将 gather_evidence 替换为其等价物（其余节点/状态 Schema/interrupt 语义一致，
      便于黄金集 A/B 对照）。架构约束：子 Agent 只读；写命令仍经领域服务+审批。
    - policy_store（可选 PolicyStore）：透传给 gather_evidence 做最小政策证据检索；
      evidence_node 提供时其覆盖优先（Supervisor 自行注入 policy_store，不受影响）。
    - max_steps：节点执行步数上限（默认 32，可构造参数覆盖）。
    """
    limit = max(1, int(max_steps))
    nodes = build_nodes(gateway, policy_store=policy_store)
    if evidence_node is not None:
        nodes["gather_evidence"] = evidence_node
        mode = "supervisor"
    g = StateGraph(AgentState)

    def bounded(name, fn):
        def run(state):
            steps = int(state.get("step_count", 0)) + 1
            if steps > limit:
                # 安全停止：本节点体不执行 → 不会被调用任何领域写操作
                logger.error(
                    "agent_loop_detected tenant=%s thread=%s node=%s step_count=%d max_steps=%d",
                    state.get("tenant_id"), state.get("thread_id"), name, steps - 1, limit,
                )
                raise AgentLoopDetected(
                    f"工作流超过最大步数 {limit}：疑似死循环（节点={name}），已安全停止并转人工")
            # 绑定 (租户, 线程, 节点) 供受控网关构造工具去重键
            with tool_call_context(state.get("tenant_id"), state.get("thread_id"), name):
                update = dict(fn(state))
            update["step_count"] = steps
            logger.debug("agent_step node=%s step_count=%d thread=%s",
                         name, steps, state.get("thread_id"))
            return update
        return run

    for name, fn in nodes.items():
        g.add_node(name, bounded(name, fn))

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
        # 领域审批事实仍为 PENDING：回到 request_approval 再次挂起等待，
        # 每次恢复都重新走 apply_decision（读领域事实 + step_count 递增），
        # 因此「重复等待」不会无限运行，也不会因为被忽略的 resume 参数而执行。
        "wait_approval": "request_approval",
    })

    for name in ("execute_operation", "settle_rejected", "escalate"):
        g.add_edge(name, END)

    return g.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())
