"""Agent 层：单 Agent 工作流（阶段 2）+ Supervisor 只读子 Agent（阶段 5B）。

职责边界：意图识别、缺参澄清、只读证据编排、动作草稿解释、审批中断与恢复；
业务真相一律来自确定性领域服务（src.domain.after_sales），checkpoint 仅存流程状态。
"""
from .graph import build_workflow
from .ports import AfterSalesGateway
from .runner import RunResult, WorkflowRunner
from .state import AgentState
from .subagents import (
    HISTORY_AGENT_SPEC,
    ORDER_AGENT_SPEC,
    POLICY_AGENT_SPEC,
    ReadOnlySubAgent,
    SubAgentSpec,
    build_supervisor_evidence,
)
from .supervisor import SupervisorRunner

__all__ = [
    "AfterSalesGateway",
    "AgentState",
    "HISTORY_AGENT_SPEC",
    "ORDER_AGENT_SPEC",
    "POLICY_AGENT_SPEC",
    "ReadOnlySubAgent",
    "RunResult",
    "SubAgentSpec",
    "SupervisorRunner",
    "WorkflowRunner",
    "build_supervisor_evidence",
    "build_workflow",
]
