"""阶段 2 单 Agent 工作流（LangGraph）。

职责边界：意图识别、缺参澄清、只读证据编排、动作草稿解释、审批中断与恢复；
业务真相一律来自确定性领域服务（src.domain.after_sales），checkpoint 仅存流程状态。
"""
from .graph import build_workflow
from .ports import AfterSalesGateway
from .runner import RunResult, WorkflowRunner
from .state import AgentState

__all__ = [
    "AfterSalesGateway",
    "AgentState",
    "RunResult",
    "WorkflowRunner",
    "build_workflow",
]
