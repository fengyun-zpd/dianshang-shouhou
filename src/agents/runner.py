"""高层运行器：start / 澄清补参 / 提交审批决定 / resume / 未知状态查询与对账。

面向测试、演示脚本与后续 API 层；审批决定只经 submit_decision 提交到领域服务，
resume 后工作流一律重读领域事实源，resume 本身不携带"通过/拒绝"的业务含义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from langgraph.types import Command

from src.domain.after_sales import AfterSalesService

from .graph import build_workflow
from .ports import AfterSalesGateway
from .state import APPROVAL_INTERRUPT_TYPE, CLARIFY_INTERRUPT_TYPE, AgentState, new_state

_ALLOWED_EXTERNAL = {"success", "timeout"}


@dataclass
class RunResult:
    """一次 invoke / resume 的结果。业务真相以领域服务查询为准（state 仅流程视图）。"""
    thread_id: str
    finished: bool
    waiting_approval: bool = False
    waiting_clarify: bool = False
    interrupt_value: Optional[dict] = None
    state: Optional[dict] = None
    outcome: Optional[str] = None
    error_code: Optional[str] = None
    reply: Optional[str] = None
    audit_event_ids: list[str] = field(default_factory=list)


class WorkflowRunner:
    """基于确定性领域服务 + LangGraph 的单 Agent 售后工作流运行器。"""

    def __init__(self, service: AfterSalesService, checkpointer=None):
        self.service = service
        self.gateway = AfterSalesGateway(service)
        self.graph = build_workflow(self.gateway, checkpointer)

    # ---------- 运行 ----------

    def start(
        self,
        tenant_id: str,
        request: str,
        thread_id: Optional[str] = None,
        order_id_hint: Optional[str] = None,
        simulate_external: str = "success",
    ) -> RunResult:
        """启动一轮售后请求处理。返回时可能已挂起（等待澄清或等待审批）。"""
        if simulate_external not in _ALLOWED_EXTERNAL:
            raise ValueError(f"simulate_external 仅允许 {_ALLOWED_EXTERNAL}")
        tid = thread_id or f"thread-{uuid4().hex[:8]}"
        inputs: dict = {
            **new_state(),
            "tenant_id": tenant_id,
            "thread_id": tid,
            "user_request": request,
            "order_id": order_id_hint,
            "simulate_external": simulate_external,
        }
        raw = self.graph.invoke(inputs, self._cfg(tid))
        return self._finalize(tid, raw)

    def resume(self, thread_id: str, payload="_continue_", simulate_external: Optional[str] = None) -> RunResult:
        """恢复被 interrupt 挂起的工作流。

        - payload：clarify 中断时为用户补参（dict：order_id/description/reason_tags 或纯文本）；
          approval 中断时为占位（默认 "_continue_"；决定以领域服务为准，该值被忽略，
          不可为 None —— langgraph 对 Command(resume=None) 存在兼容问题）。
        - simulate_external：可选覆盖执行阶段的模拟结果（仅测试/演示）。
        """
        cfg = self._cfg(thread_id)
        if simulate_external is not None:
            if simulate_external not in _ALLOWED_EXTERNAL:
                raise ValueError(f"simulate_external 仅允许 {_ALLOWED_EXTERNAL}")
            self.graph.update_state(cfg, {"simulate_external": simulate_external})
        raw = self.graph.invoke(Command(resume=payload), cfg)
        return self._finalize(thread_id, raw)

    def get_state(self, thread_id: str) -> RunResult:
        snap = self.graph.get_state(self._cfg(thread_id))
        values = dict(snap.values)
        interrupts = getattr(snap, "interrupts", None) or ()
        return self._build_result(thread_id, values, interrupts)

    # ---------- 审批（授权人员提交决定到领域事实源） ----------

    def submit_decision(self, operation_id: str, decision: str, reason: Optional[str] = None):
        """仅 APPROVER 语义：向领域服务提交 approve / reject 决定（带版本校验）。"""
        return self.gateway.submit_approver_decision(operation_id, decision, reason)

    # ---------- operation_unknown（只允许原 operation_id 查询/对账） ----------

    def query_operation(self, operation_id: str):
        """只读查询：以原 operation_id 查询外部结果（禁止换键重试）。"""
        return self.gateway.get_operation(operation_id)

    def reconcile_unknown(self, operation_id: str, result: str):
        """以原 operation_id 对账收口：success → executed / failed → failed。"""
        return self.gateway.reconcile(operation_id, result)

    # ---------- 内部 ----------

    def _cfg(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    def _finalize(self, thread_id: str, raw: dict) -> RunResult:
        interrupts = raw.get("__interrupt__") or ()
        values = {k: v for k, v in raw.items() if k != "__interrupt__"}
        return self._build_result(thread_id, values, interrupts)

    def _build_result(self, thread_id: str, values: dict, interrupts) -> RunResult:
        iv = None
        waiting_approval = False
        waiting_clarify = False
        if interrupts:
            first = interrupts[0]
            iv = dict(first.value) if getattr(first, "value", None) else {"type": "unknown"}
            waiting_approval = iv.get("type") == APPROVAL_INTERRUPT_TYPE
            waiting_clarify = iv.get("type") == CLARIFY_INTERRUPT_TYPE
        return RunResult(
            thread_id=thread_id,
            finished=not interrupts,
            waiting_approval=waiting_approval,
            waiting_clarify=waiting_clarify,
            interrupt_value=iv,
            state=values,
            outcome=values.get("outcome"),
            error_code=values.get("error_code"),
            reply=values.get("reply"),
            audit_event_ids=list(values.get("audit_event_ids") or []),
        )
