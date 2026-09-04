"""高层运行器：start / 澄清补参 / 提交审批决定 / resume / 未知状态查询与对账。

面向测试、演示脚本与后续 API 层；审批决定只经 submit_decision 提交到领域服务，
resume 后工作流一律重读领域事实源，resume 本身不携带"通过/拒绝"的业务含义。

线程生命周期（任务卡 J）：同一 thread_id 只能继续原请求（请求指纹写入 checkpoint），
已结束线程提交不同请求被拒绝；同请求重复提交返回原结果（不重放）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from langgraph.types import Command

from src.domain.after_sales import AfterSalesService

from .ports import AfterSalesGateway
from .state import APPROVAL_INTERRUPT_TYPE, CLARIFY_INTERRUPT_TYPE, AgentState, new_state

_ALLOWED_EXTERNAL = {"success", "timeout"}


class ThreadConflictError(ValueError):
    """线程生命周期冲突：同一 thread 提交不同请求 / 复用无指纹线程 / 未绑定租户。"""


def _make_request_fingerprint(tenant_id: str, request: str, order_id_hint: Optional[str]) -> dict:
    """请求指纹：tenant + 规范化请求 + 订单提示。租户绑定不可变且进入指纹。"""
    norm = "|".join([tenant_id, order_id_hint or "", (request or "").strip()])
    return {
        "tenant_id": tenant_id,
        "request_hash": hashlib.sha256(norm.encode("utf-8")).hexdigest(),
    }


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
        self._thread_tenants: dict[str, str] = {}
        self.graph = self._build_graph(checkpointer)

    def _build_graph(self, checkpointer=None):
        """构建默认单 Agent 图（子类可覆盖以切换运行时，如 Supervisor 模式）。"""
        from .graph import build_workflow
        return build_workflow(self.gateway, checkpointer)

    # ---------- 运行 ----------

    def start(
        self,
        tenant_id: str,
        request: str,
        thread_id: Optional[str] = None,
        order_id_hint: Optional[str] = None,
        simulate_external: str = "success",
    ) -> RunResult:
        """启动一轮售后请求处理。返回时可能已挂起（等待澄清或等待审批）。

        线程生命周期：
        - 新 thread → 记录请求指纹并运行；
        - 已有 thread（同指纹）→ 返回当前/原结果（同请求重复提交，不重放）；
        - 已有 thread 且请求指纹不同 → ThreadConflictError（禁止同一线程提交不同请求）。
        """
        if simulate_external not in _ALLOWED_EXTERNAL:
            raise ValueError(f"simulate_external 仅允许 {_ALLOWED_EXTERNAL}")
        tid = thread_id or f"thread-{uuid4().hex[:8]}"
        self._bind_thread(tid, tenant_id)
        cfg = self._cfg(tid)
        fp = _make_request_fingerprint(tenant_id, request, order_id_hint)

        existing = self._peek_thread(cfg)
        if existing is not None:
            values, interrupts = existing
            old_fp = values.get("thread_request_fingerprint")
            if old_fp is None:
                raise ThreadConflictError(
                    f"线程 {tid} 无请求指纹（疑似跨版本/损坏 checkpoint），拒绝复用")
            if old_fp != fp:
                raise ThreadConflictError(
                    f"线程 {tid} 已被其他请求占用（指纹不一致）：同一 thread 只能继续原请求")
            # 同请求重复提交 → 返回原/当前结果（不重放、无新副作用）
            return self._build_result(tid, values, interrupts)

        inputs: dict = {
            **new_state(),
            "tenant_id": tenant_id,
            "thread_id": tid,
            "user_request": request,
            "order_id": order_id_hint,
            "simulate_external": simulate_external,
            "thread_request_fingerprint": fp,
        }
        raw = self.graph.invoke(inputs, cfg)
        return self._finalize(tid, raw)

    def resume(self, thread_id: str, payload="_continue_", simulate_external: Optional[str] = None,
               tenant_id: Optional[str] = None) -> RunResult:
        """恢复被 interrupt 挂起的工作流。

        - payload：clarify 中断时为用户补参（dict：order_id/description/reason_tags 或纯文本）；
          approval 中断时为占位（默认 "_continue_"；决定以领域服务为准，该值被忽略，
          不可为 None —— langgraph 对 Command(resume=None) 存在兼容问题）。
        - simulate_external：可选覆盖执行阶段的模拟结果（仅测试/演示）。
        """
        self._bind_thread(thread_id, tenant_id)
        cfg = self._cfg(thread_id)
        if simulate_external is not None:
            if simulate_external not in _ALLOWED_EXTERNAL:
                raise ValueError(f"simulate_external 仅允许 {_ALLOWED_EXTERNAL}")
            self.graph.update_state(cfg, {"simulate_external": simulate_external})
        raw = self.graph.invoke(Command(resume=payload), cfg)
        return self._finalize(thread_id, raw)

    def get_state(self, thread_id: str, tenant_id: Optional[str] = None) -> RunResult:
        self._bind_thread(thread_id, tenant_id)
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

    def _peek_thread(self, cfg: dict):
        """查看线程当前 checkpoint（不存在返回 None；存在返回 (values, interrupts)）。"""
        snap = self.graph.get_state(cfg)
        values = dict(snap.values or {})
        interrupts = tuple(getattr(snap, "interrupts", None) or ())
        if not values:
            return None
        return values, interrupts

    def _cfg(self, thread_id: str) -> dict:
        tenant_id = self._thread_tenants.get(thread_id)
        if tenant_id is None:
            raise ValueError(f"UNKNOWN_THREAD: 线程 {thread_id} 未绑定租户")
        # LangGraph 默认以 thread_id 做 checkpoint 主键，必须把租户纳入键空间。
        return {"configurable": {"thread_id": f"{tenant_id}:{thread_id}"}}

    def _bind_thread(self, thread_id: str, tenant_id: Optional[str]) -> str:
        """绑定并校验线程租户；checkpoint 中的 tenant_id 不能被调用方覆盖。"""
        bound = self._thread_tenants.get(thread_id)
        if bound is not None and tenant_id is not None and bound != tenant_id:
            raise ValueError(f"THREAD_TENANT_CONFLICT: 线程 {thread_id} 已绑定租户 {bound}")
        effective = bound or tenant_id
        if effective is None:
            raise ValueError(f"UNKNOWN_THREAD: 线程 {thread_id} 未在当前运行器注册，请提供 tenant_id")
        self._thread_tenants[thread_id] = effective
        return effective

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
