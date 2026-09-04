"""Supervisor 模式运行器（阶段 5B，ADR-002 实验）。

与单 Agent WorkflowRunner 完全相同的 API / AgentState / interrupt-resume / 审批语义；
唯一差异：证据编排节点由 Supervisor 并行调度只读子 Agent（order/history/policy）。

- Supervisor 子 Agent 只有只读工具白名单，无写路径；
- 写命令（建单/草稿/提交/执行/关单）仍由顶层 Supervisor 经网关 → 领域服务 + 人工审批；
- checkpoint 仅存流程状态，业务真相仍重读领域服务。
"""
from __future__ import annotations

from typing import Optional

from src.rag import PolicyStore

from .runner import WorkflowRunner


class SupervisorRunner(WorkflowRunner):
    """Supervisor 模式（子 Agent 只读证据编排）。"""

    def __init__(self, service, policy_store: Optional[PolicyStore] = None,
                 checkpointer=None):
        self.policy_store = policy_store
        super().__init__(service, checkpointer)

    def _build_graph(self, checkpointer=None):
        from .graph import build_workflow
        from .subagents import build_supervisor_evidence
        evidence = build_supervisor_evidence(self.gateway, self.policy_store)
        return build_workflow(self.gateway, checkpointer,
                              evidence_node=evidence, mode="supervisor")
