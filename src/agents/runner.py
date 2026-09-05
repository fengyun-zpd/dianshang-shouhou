"""高层运行器：start / 澄清补参 / 提交审批决定 / resume / 未知状态查询与对账。

面向测试、演示脚本与后续 API 层；审批决定只经 submit_decision 提交到领域服务，
resume 后工作流一律重读领域事实源，resume 本身不携带"通过/拒绝"的业务含义。

后端依赖：本运行器只依赖 AfterSalesApplicationPort（MemoryAdapter / PgCommandAdapter
均可，不 isinstance）——构造传入 Port 实现并交给 AfterSalesGateway 使用。

线程生命周期（任务卡 J）：同一 thread_id 只能继续原请求（请求指纹写入 checkpoint），
已结束线程提交不同请求被拒绝；同请求重复提交返回原结果（不重放）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from langgraph.types import Command

from .ports import AfterSalesGateway
from .state import APPROVAL_INTERRUPT_TYPE, CLARIFY_INTERRUPT_TYPE, AgentState, new_state

_ALLOWED_EXTERNAL = {"success", "timeout"}


class ThreadConflictError(ValueError):
    """线程生命周期冲突：同一 thread 提交不同请求 / 复用无指纹线程 / 未绑定租户。"""


class ThreadLeaseError(RuntimeError):
    """未持有 workflow_threads 数据库租约（他人持约未过期 / request_fingerprint 不符）：
    禁止读 checkpoint、推进图或产生任何副作用。"""


def _lease_fingerprint_str(tenant_id: str, request: str, order_id_hint: Optional[str]) -> str:
    """租约 request_fingerprint：由 tenant/request/order 确定性生成（禁止空串）。"""
    import json as _json
    return _json.dumps(_make_request_fingerprint(tenant_id, request, order_id_hint), sort_keys=True)


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
    """基于 AfterSalesApplicationPort + LangGraph 的单 Agent 售后工作流运行器。

    backend：实现 AfterSalesApplicationPort 的后端（MemoryAdapter / PgCommandAdapter）。
    lease_repo（可选，D9 硬租约）：提供 AfterSalesRepository 的 workflow_threads 租约。
    提供时 start/resume 必须在读 checkpoint、graph.update_state/invoke 与任何副作用前
    原子获得租约（owner_id 稳定唯一）；失约抛 ThreadLeaseError。线程完成（finished）或
    异常路径由 owner 释放租约；等待审批（interrupt）期间保持租约，崩溃后可由同
    fingerprint 且租约过期的其他 owner 接管。
    policy_store（可选 PolicyStore）：注入 gather_evidence 的最小政策证据检索
    （证据引用不裁决金额/资格；不提供时行为与基线一致）。
    """

    def __init__(self, backend, checkpointer=None,
                 lease_repo=None, owner_id: Optional[str] = None,
                 lease_duration_s: int = 60,
                 policy_store=None):
        self.backend = backend
        self.gateway = AfterSalesGateway(backend)
        self._thread_tenants: dict[str, str] = {}
        self._op_tenants: dict[str, str] = {}  # 本运行器见过的 operation_id -> tenant_id
        self._lease_repo = lease_repo
        if lease_repo is not None and not owner_id:
            raise ValueError("启用线程租约必须提供稳定 owner_id")
        self._owner_id = owner_id
        self._lease_duration_s = lease_duration_s
        # 可选政策证据检索（PolicyStore）；None → gather_evidence 与既有基线一致
        self._policy_store = policy_store
        self.graph = self._build_graph(checkpointer)

    # ---------- 租约（D9 硬边界） ----------

    def _lease_enabled(self) -> bool:
        return self._lease_repo is not None

    def _acquire_lease(self, tenant_id: str, thread_id: str, fingerprint: str) -> None:
        """原子获租/续租/接管；失败 → ThreadLeaseError（不读 checkpoint、不推进图）。"""
        ok = self._lease_repo.claim_thread(
            tenant_id, thread_id, self._owner_id, self._lease_duration_s, fingerprint)
        if not ok:
            raise ThreadLeaseError(
                f"线程 {thread_id} 未获租约：他人持约未过期或 request_fingerprint 不符（拒绝覆盖）")

    def _release_lease(self, tenant_id: str, thread_id: str) -> None:
        """仅当前 owner 释放；不匹配（他人已接管）则保持他人租约。"""
        self._lease_repo.release_thread(tenant_id, thread_id, self._owner_id)

    def _existing_thread_fingerprint(self, tenant_id: str, thread_id: str) -> str:
        row = self._lease_repo.get_thread(tenant_id, thread_id)
        if row is None or not row[0]:
            raise ThreadLeaseError(f"线程 {thread_id} 无 workflow_threads 事实，无法续租")
        return row[0]

    def _build_graph(self, checkpointer=None):
        """构建默认单 Agent 图（子类可覆盖以切换运行时，如 Supervisor 模式）。"""
        from .graph import build_workflow
        return build_workflow(self.gateway, checkpointer, policy_store=self._policy_store)

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
        acquired = False
        result = None
        if self._lease_enabled():
            # 持约后才允许读 checkpoint / invoke（失约即拒绝，异请求不覆盖既有事实）
            self._acquire_lease(tenant_id, tid, _lease_fingerprint_str(tenant_id, request, order_id_hint))
            acquired = True
        try:
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
                result = self._build_result(tid, values, interrupts)
                return result

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
            result = self._finalize(tid, raw)
            return result
        finally:
            if acquired and (result is None or result.finished):
                # 异常（result 未赋值）或线程完成 → 由 owner 释放；等待（interrupt）保持租约
                self._release_lease(tenant_id, tid)

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
        acquired = False
        result = None
        if self._lease_enabled():
            # 持约后才允许 update_state / 读 checkpoint / invoke；指纹取 workflow_threads 既有事实
            fp = self._existing_thread_fingerprint(self._thread_tenants[thread_id], thread_id)
            self._acquire_lease(self._thread_tenants[thread_id], thread_id, fp)
            acquired = True
        try:
            if simulate_external is not None:
                if simulate_external not in _ALLOWED_EXTERNAL:
                    raise ValueError(f"simulate_external 仅允许 {_ALLOWED_EXTERNAL}")
                self.graph.update_state(cfg, {"simulate_external": simulate_external})
            raw = self.graph.invoke(Command(resume=payload), cfg)
            result = self._finalize(thread_id, raw)
            return result
        finally:
            if acquired and (result is None or result.finished):
                self._release_lease(self._thread_tenants[thread_id], thread_id)

    def get_state(self, thread_id: str, tenant_id: Optional[str] = None) -> RunResult:
        self._bind_thread(thread_id, tenant_id)
        snap = self.graph.get_state(self._cfg(thread_id))
        values = dict(snap.values)
        interrupts = getattr(snap, "interrupts", None) or ()
        return self._build_result(thread_id, values, interrupts)

    # ---------- 审批（授权人员提交决定到领域事实源） ----------

    def submit_decision(self, operation_id: str, decision: str, reason: Optional[str] = None,
                        tenant_id: Optional[str] = None):
        """仅 APPROVER 语义：向领域服务提交 approve / reject 决定（带版本校验）。

        tenant_id 优先取显式参数；未提供时回退到本运行器在运行中见过的
        operation_id→tenant 登记（该操作由本运行器 start/resume 产生时）。
        两者皆无 → ValueError（不静默猜测租户）。
        """
        tenant = tenant_id or self._op_tenants.get(operation_id)
        if tenant is None:
            raise ValueError(
                f"UNKNOWN_OPERATION_TENANT: 操作 {operation_id} 未登记租户，请显式传 tenant_id")
        return self.gateway.submit_approver_decision(tenant, operation_id, decision, reason)

    # ---------- operation_unknown（只允许原 operation_id 查询/对账） ----------

    def query_operation(self, operation_id: str, tenant_id: Optional[str] = None):
        """只读查询：以原 operation_id 查询外部结果（禁止换键重试）。"""
        tenant = tenant_id or self._op_tenants.get(operation_id)
        if tenant is None:
            raise ValueError(
                f"UNKNOWN_OPERATION_TENANT: 操作 {operation_id} 未登记租户，请显式传 tenant_id")
        return self.gateway.get_operation(tenant, operation_id)

    def reconcile_unknown(self, operation_id: str, result: str,
                          tenant_id: Optional[str] = None):
        """以原 operation_id 对账收口：success → executed / failed → failed。"""
        tenant = tenant_id or self._op_tenants.get(operation_id)
        if tenant is None:
            raise ValueError(
                f"UNKNOWN_OPERATION_TENANT: 操作 {operation_id} 未登记租户，请显式传 tenant_id")
        return self.gateway.reconcile(tenant, operation_id, result)

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
        # 登记本运行器见过的操作 → 租户（供 submit_decision/query/reconcile 无参回退）
        tenant = self._thread_tenants.get(thread_id)
        op_id = values.get("operation_id")
        if tenant and op_id:
            self._op_tenants[op_id] = tenant
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
