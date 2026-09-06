"""Agent 层：单 Agent 工作流（阶段 2）+ Supervisor 只读子 Agent（阶段 5B / V1.1）。

职责边界：意图识别、缺参澄清、只读证据编排、动作草稿解释、审批中断与恢复；
业务真相一律来自确定性领域服务（src.domain.after_sales），checkpoint 仅存流程状态。

Supervisor 编排（src/agents/supervisor.py）：
- 默认 orchestration="three-agent"：三只读子 Agent（subagents.py，V1 对照语义）；
- 可选 orchestration="four-role"：四角色只读流程 Triage → Evidence → Resolution →
  RiskReview（multiagent.py，V1.1 新增；规则实现，非真实 LLM）。
"""
from .graph import build_workflow
from .multiagent import (
    EVIDENCE_AGENT_SPEC,
    RESOLUTION_AGENT_SPEC,
    RISK_REVIEWER_SPEC,
    TRIAGE_AGENT_SPEC,
    FourRoleOrchestrator,
    RoleAgent,
    RoleAgentSpec,
    build_four_role_orchestrator,
)
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
    "EVIDENCE_AGENT_SPEC",
    "FourRoleOrchestrator",
    "HISTORY_AGENT_SPEC",
    "ORDER_AGENT_SPEC",
    "POLICY_AGENT_SPEC",
    "RESOLUTION_AGENT_SPEC",
    "RISK_REVIEWER_SPEC",
    "ReadOnlySubAgent",
    "RoleAgent",
    "RoleAgentSpec",
    "RunResult",
    "SubAgentSpec",
    "SupervisorRunner",
    "TRIAGE_AGENT_SPEC",
    "WorkflowRunner",
    "build_four_role_orchestrator",
    "build_supervisor_evidence",
    "build_workflow",
]
