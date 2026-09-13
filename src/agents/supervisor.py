"""Supervisor 模式运行器（阶段 5B ADR-002 实验 / V1.1 四角色可选编排）。

与单 Agent WorkflowRunner 完全相同的 API / AgentState / interrupt-resume / 审批语义；
差异仅在证据编排节点由 Supervisor 调度只读子 Agent：

- orchestration="three-agent"（默认）：并行调度 order/history/policy 三只读子 Agent
  （build_supervisor_evidence，V1 对照语义；evals/compare_agents.py 依赖此默认）；
- orchestration="four-role"（V1.1 新增可选）：四角色只读编排 Triage → Evidence →
  Resolution → RiskReview（src/agents/multiagent.py），产出与单 Agent
  gather_evidence+plan 等价的 AgentState 语义，便于同一黄金集 A/B 对照。

边界（两编排相同，宪法第三/四条）：
- 子 Agent/角色只有只读工具白名单，无写路径；
- 写命令（建单/草稿/提交/执行/关单）仍由顶层 Supervisor 经网关 → 领域服务 + 人工审批；
- checkpoint 仅存流程状态，业务真相仍重读领域服务。
"""
from __future__ import annotations

from typing import Optional

from src.rag import PolicyStore

from .graph import DEFAULT_MAX_STEPS
from .runner import WorkflowRunner

_ORCHESTRATIONS = ("three-agent", "four-role")


class SupervisorRunner(WorkflowRunner):
    """Supervisor 模式（子 Agent 只读证据编排）。"""

    def __init__(self, service, policy_store: Optional[PolicyStore] = None,
                 checkpointer=None, orchestration: str = "three-agent",
                 max_steps: int = DEFAULT_MAX_STEPS):
        if orchestration not in _ORCHESTRATIONS:
            raise ValueError(f"未知 Supervisor 编排：{orchestration!r}（可选 {_ORCHESTRATIONS}）")
        self.policy_store = policy_store
        self.orchestration = orchestration
        super().__init__(service, checkpointer, max_steps=max_steps)

    def _build_graph(self, checkpointer=None):
        from .graph import build_workflow
        if self.orchestration == "four-role":
            from .multiagent import build_four_role_orchestrator
            evidence = build_four_role_orchestrator(self.gateway, self.policy_store)
        else:
            from .subagents import build_supervisor_evidence
            evidence = build_supervisor_evidence(self.gateway, self.policy_store)
        return build_workflow(self.gateway, checkpointer,
                              evidence_node=evidence, mode="supervisor",
                              max_steps=self._max_steps)
