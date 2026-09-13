"""高层运行器：start / 澄清补参 / 提交审批决定 / resume / 未知状态查询与对账。

面向测试、演示脚本与 HTTP 接口层（`/api/v1/agent/*` 只做转发）；
审批决定只经 submit_decision 或领域审批接口提交到领域服务，resume 后工作流一律重读
领域事实源，resume 本身不携带"通过/拒绝"的业务含义。

后端依赖：本运行器只依赖 AfterSalesApplicationPort（MemoryAdapter / PgCommandAdapter
均可，不 isinstance）——构造传入 Port 实现并交给 AfterSalesGateway 使用。

线程生命周期（任务卡 J）：同一 thread_id 只能继续原请求（请求指纹写入 checkpoint），
已结束线程提交不同请求被拒绝；同请求重复提交返回原结果（不重放）。

确定性保护（不依赖模型自觉，V1.2）：
- 节点步数上限（默认 32，可构造参数覆盖）+ LangGraph recursion limit 双重保护；
  超出 → AgentLoopDetected → outcome=escalated / error_code=AGENT_LOOP_DETECTED，
  在调用任何领域写操作之前安全停止；**终态标记写入持久 checkpoint**，因此进程重启后的
  新实例仍能读到 AGENT_LOOP_DETECTED / escalated / 人工接管原因；
- 工具去重账本（ToolCallLedger）：受控写按（工具名, 租户, thread_id, 参数摘要）复用
  首次结果；`get_operation` 等事实重读工具永不缓存（审批以领域事实为准）；
  重复副作用的最终兜底仍是领域服务幂等键。

线程作用域（V1.2 收紧）：线程唯一键是 (tenant_id, thread_id)。
- PG profile（lease_repo）：绑定事实源是**租户限定的 workflow_threads 行** + 持久 checkpoint，
  进程内字典只是便利缓存，因此重启后新实例仍能恢复绑定；
- 内存 profile：进程内绑定 + checkpoint 事实，仅用于合成演示，**不声称跨进程持久恢复**；
- 不存在的线程与错误租户返回同一个 UnknownThreadError（对外统一 404），不泄露所属租户；
- 同名线程在不同租户下互不冲突（checkpoint 键空间为 v2 长度编码，避免标识符中的冒号碰撞）。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from src.domain.after_sales.models import OperationStatus

from .graph import AgentLoopDetected, DEFAULT_MAX_STEPS, LOOP_ERROR_CODE
from .ports import AfterSalesGateway
from .state import APPROVAL_INTERRUPT_TYPE, CLARIFY_INTERRUPT_TYPE, AgentState, new_state
from .tool_ledger import ToolCallLedger

_ALLOWED_EXTERNAL = {"success", "timeout"}
_CHECKPOINT_KEY_VERSION = "v2"


def _checkpoint_thread_key(tenant_id: str, thread_id: str) -> str:
    """Encode the tenant/thread pair without delimiter ambiguity.

    Length prefixes keep identifiers containing ':' (or the separator itself)
    unambiguous while retaining a readable key for local checkpoint inspection.
    """
    return (f"{_CHECKPOINT_KEY_VERSION}|{len(tenant_id)}|{tenant_id}|"
            f"{len(thread_id)}|{thread_id}")


def _legacy_checkpoint_thread_key(tenant_id: str, thread_id: str) -> str:
    """The pre-V1.2 key, used only after exact state metadata validation."""
    return f"{tenant_id}:{thread_id}"
logger = logging.getLogger("opspilot.agent")


class ThreadConflictError(ValueError):
    """线程生命周期冲突：同一 (租户, thread) 提交不同请求 / 复用无指纹线程。"""


class UnknownThreadError(ValueError):
    """线程不存在或不属于当前租户。

    对外统一映射为 404（不区分「不存在」与「属于其他租户」），
    消息中**不得**出现所属租户，避免跨租户存在性泄露。
    """


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
    max_steps：工作流节点步数上限（默认 32）——超出即安全停止（AGENT_LOOP_DETECTED、
    outcome=escalated、触发后无新增领域写入、不执行退款），不依赖模型自觉判断。
    工具去重账本（ToolCallLedger）由本运行器创建并注入网关：受控写工具按
    (工具名, 租户, thread_id, 参数摘要) 复用首次结果；审批事实重读永不缓存。
    """

    def __init__(self, backend, checkpointer=None,
                 lease_repo=None, owner_id: Optional[str] = None,
                 lease_duration_s: int = 60,
                 policy_store=None, max_steps: int = DEFAULT_MAX_STEPS):
        self.backend = backend
        self._ledger = ToolCallLedger()
        self.gateway = AfterSalesGateway(backend, ledger=self._ledger)
        # 线程绑定**便利缓存**（不是事实源）：PG profile 以 workflow_threads 为准，
        # 内存 profile 以进程内绑定 + checkpoint 事实为准。
        self._bound: set[tuple[str, str]] = set()
        self._recent_tenant: dict[str, str] = {}
        self._op_tenants: dict[str, str] = {}  # 本运行器见过的 operation_id -> tenant_id
        self._lease_repo = lease_repo
        if lease_repo is not None and not owner_id:
            raise ValueError("启用线程租约必须提供稳定 owner_id")
        self._owner_id = owner_id
        self._lease_duration_s = lease_duration_s
        # 可选政策证据检索（PolicyStore）；None → gather_evidence 与既有基线一致
        self._policy_store = policy_store
        self._max_steps = max(1, int(max_steps))
        # 第二道防线：LangGraph recursion limit 只兜「单次 invoke 内的超级步循环」。
        # 取 max_steps*2+10，保证节点包装器（确定性 step_count）先触发。
        self._recursion_limit = max(25, self._max_steps * 2 + 10)
        self.graph = self._build_graph(checkpointer)

    @property
    def max_steps(self) -> int:
        return self._max_steps

    @property
    def tool_ledger(self) -> ToolCallLedger:
        return self._ledger

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

    # ---------- 线程作用域解析 (tenant_id, thread_id) ----------

    @staticmethod
    def _unknown_message(thread_id: str) -> str:
        """通用消息：不区分「不存在」与「属于其他租户」，也不含所属租户。"""
        return f"UNKNOWN_THREAD: 线程 {thread_id} 不存在或不属于当前租户"

    def _remember(self, tenant_id: str, thread_id: str) -> None:
        self._bound.add((tenant_id, thread_id))
        self._recent_tenant[thread_id] = tenant_id

    def _checkpoint_exists(self, tenant_id: str, thread_id: str) -> bool:
        """只读探测该 (tenant, thread) 在 checkpoint 中是否已有流程事实。"""
        try:
            snap = self.graph.get_state(self._cfg_for(tenant_id, thread_id))
        except Exception:  # noqa: BLE001  探测失败按不存在处理（不泄露、不误判）
            return False
        return bool(snap.values or {})

    def _thread_exists(self, tenant_id: str, thread_id: str) -> bool:
        """(tenant_id, thread_id) 是否为本运行器可服务的线程。"""
        if self._lease_repo is not None:
            # PG profile：事实源是租户限定的 workflow_threads 行（跨进程可恢复）
            return self._lease_repo.get_thread(tenant_id, thread_id) is not None
        return (tenant_id, thread_id) in self._bound or self._checkpoint_exists(tenant_id, thread_id)

    def _resolve_tenant(self, thread_id: str, tenant_id: Optional[str]) -> str:
        """解析线程归属租户；不存在或错误租户 → UnknownThreadError（通用 404）。

        tenant_id 省略时仅允许**本进程内唯一已知**的绑定（脚本/单测便利）；
        PG profile 下未显式提供租户且本进程无缓存 → 直接 404（绝不猜测租户）。
        """
        if tenant_id is not None:
            if self._thread_exists(tenant_id, thread_id):
                self._remember(tenant_id, thread_id)
                return tenant_id
            raise UnknownThreadError(self._unknown_message(thread_id))
        cached = self._recent_tenant.get(thread_id)
        if cached is not None and self._thread_exists(cached, thread_id):
            return cached
        raise UnknownThreadError(self._unknown_message(thread_id))

    def _build_graph(self, checkpointer=None):
        """构建默认单 Agent 图（子类可覆盖以切换运行时，如 Supervisor 模式）。"""
        from .graph import build_workflow
        return build_workflow(self.gateway, checkpointer,
                              policy_store=self._policy_store,
                              max_steps=self._max_steps)

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

        线程生命周期（作用域 = (tenant_id, thread_id)）：
        - 新 (租户, thread) → 记录请求指纹并运行；同名线程在别的租户下互不影响；
        - 已有且同请求指纹 → 返回当前/原结果（同请求重复提交，不重放）；
        - 已有且请求指纹不同 → ThreadConflictError（禁止同一线程提交不同请求）。

        simulate_external：**仅测试与合成演示**可注入外部执行结果；
        该参数不在 HTTP schema 中（`/api/v1/agent/start` 不可达），
        生产路径的外部结果只能由 SYSTEM 角色的领域执行接口写入。
        """
        if simulate_external not in _ALLOWED_EXTERNAL:
            raise ValueError(f"simulate_external 仅允许 {_ALLOWED_EXTERNAL}")
        tid = thread_id or f"thread-{uuid4().hex[:8]}"
        cfg = self._cfg_for(tenant_id, tid)
        fp = _make_request_fingerprint(tenant_id, request, order_id_hint)
        acquired = False
        result = None
        if self._lease_enabled():
            self._assert_request_matches_existing(tenant_id, tid, request, order_id_hint)
            # 持约后才允许读 checkpoint / invoke（失约即拒绝，异请求不覆盖既有事实）
            self._acquire_lease(tenant_id, tid,
                                _lease_fingerprint_str(tenant_id, request, order_id_hint))
            acquired = True
        self._remember(tenant_id, tid)
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
                result = self._build_result(tenant_id, tid, values, interrupts)
                return self._recover_terminal_state(tenant_id, tid, result, cfg)

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
            result = self._finalize(tenant_id, tid, raw)
            return self._recover_terminal_state(tenant_id, tid, result, cfg)
        except AgentLoopDetected as exc:
            result = self._loop_stop(tenant_id, tid, exc, cfg)
            return result
        except GraphRecursionError as exc:
            result = self._loop_stop(tenant_id, tid, exc, cfg)
            return result
        finally:
            if acquired and (result is None or result.finished):
                # 异常（result 未赋值）或线程完成 → 由 owner 释放；等待（interrupt）保持租约
                self._release_lease(tenant_id, tid)

    def _assert_request_matches_existing(self, tenant_id: str, thread_id: str,
                                         request: str, order_id_hint: Optional[str]) -> None:
        """租约启用时的先验校验：同 (租户, thread) 异请求必须报线程冲突而非租约错误。"""
        row = self._lease_repo.get_thread(tenant_id, thread_id)
        if row is None or not row[0]:
            return
        expected = _lease_fingerprint_str(tenant_id, request, order_id_hint)
        if row[0] != expected:
            raise ThreadConflictError(
                f"线程 {thread_id} 已被其他请求占用（指纹不一致）：同一 thread 只能继续原请求")

    def resume(self, thread_id: str, payload="_continue_", simulate_external: Optional[str] = None,
               tenant_id: Optional[str] = None) -> RunResult:
        """恢复被 interrupt 挂起的工作流。

        - payload：clarify 中断时为用户补参（dict：order_id/description/reason_tags 或纯文本）；
          approval 中断时为占位（默认 "_continue_"；决定以领域服务为准，该值被忽略，
          不可为 None —— langgraph 对 Command(resume=None) 存在兼容问题）。
        - simulate_external：可选覆盖执行阶段的模拟结果（**仅测试与合成演示**）。
        """
        tenant = self._resolve_tenant(thread_id, tenant_id)
        cfg = self._cfg_for(tenant, thread_id)
        acquired = False
        result = None
        if self._lease_enabled():
            # 持约后才允许 update_state / 读 checkpoint / invoke；指纹取 workflow_threads 既有事实
            fp = self._existing_thread_fingerprint(tenant, thread_id)
            self._acquire_lease(tenant, thread_id, fp)
            acquired = True
        try:
            existing = self._peek_thread(cfg)
            if existing is not None:
                values, interrupts = existing
                if not interrupts and values.get("next_action") in {"reconcile_required", "finished"}:
                    result = self._recover_terminal_state(
                        tenant, thread_id, self._build_result(tenant, thread_id, values, interrupts), cfg)
                    return result
            if simulate_external is not None:
                if simulate_external not in _ALLOWED_EXTERNAL:
                    raise ValueError(f"simulate_external 仅允许 {_ALLOWED_EXTERNAL}")
                self.graph.update_state(cfg, {"simulate_external": simulate_external})
            raw = self.graph.invoke(Command(resume=payload), cfg)
            result = self._finalize(tenant, thread_id, raw)
            return self._recover_terminal_state(tenant, thread_id, result, cfg)
        except AgentLoopDetected as exc:
            result = self._loop_stop(tenant, thread_id, exc, cfg)
            return result
        except GraphRecursionError as exc:
            result = self._loop_stop(tenant, thread_id, exc, cfg)
            return result
        finally:
            if acquired and (result is None or result.finished):
                self._release_lease(tenant, thread_id)

    def get_state(self, thread_id: str, tenant_id: Optional[str] = None) -> RunResult:
        """只读查询当前线程视图（不创建新线程；不因调用方给了租户就返回空状态）。

        只读语义：未知线程与错误租户都返回同一个 UnknownThreadError（通用 404），
        调用方无法据此判断该线程是否存在于其他租户。
        """
        tenant = self._resolve_tenant(thread_id, tenant_id)
        snap = self.graph.get_state(self._cfg_for(tenant, thread_id))
        values = dict(snap.values)
        interrupts = getattr(snap, "interrupts", None) or ()
        return self._build_result(tenant, thread_id, values, interrupts)

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

    def _recover_terminal_state(self, tenant_id: str, thread_id: str,
                                result: RunResult, cfg: dict) -> RunResult:
        """Re-read settled domain facts and finish an old workflow snapshot.

        A graph can already be at END when an external executor or a
        reconciliation call changes the operation. Recovery therefore updates
        only the workflow view and invokes the existing domain close command;
        it never executes or creates a new operation.
        """
        state = dict(result.state or {})
        if state.get("error_code") == LOOP_ERROR_CODE:
            return result
        operation_id = state.get("operation_id")
        ticket_id = state.get("ticket_id")
        if not operation_id or not ticket_id:
            return result
        try:
            operation = self.gateway.get_operation(tenant_id, operation_id)
        except Exception:  # noqa: BLE001 - preserve the original workflow view
            return result
        if operation.status not in {
            OperationStatus.EXECUTED, OperationStatus.REJECTED, OperationStatus.FAILED,
        }:
            return result
        try:
            ticket = self.gateway.get_ticket_for(tenant_id, ticket_id)
            # Executed/rejected are safe terminal dispositions. A failed
            # reconciliation remains open for manual handling and must not be
            # represented as a successful close.
            if (operation.status in {OperationStatus.EXECUTED, OperationStatus.REJECTED}
                    and ticket.status.value != "closed"):
                self.gateway.close_ticket(tenant_id, ticket_id)
            ids = self.gateway.collect_audit_events(
                tenant_id, thread_id, (ticket_id, operation_id), state.get("audit_event_ids"))
        except Exception as exc:  # noqa: BLE001 - retain structured failure, never fake success
            code = getattr(getattr(exc, "code", None), "value", "AGENT_TERMINAL_RECOVERY_FAILED")
            failed_state = {
                **state,
                "error_code": code,
                "outcome": "escalated",
                "next_action": "escalated",
                "reply": f"业务结果已写入但工单收尾失败，需人工处理（{code}）",
            }
            self.graph.update_state(cfg, failed_state, as_node="escalate")
            return self._build_result(tenant_id, thread_id, failed_state, ())
        if operation.status == OperationStatus.EXECUTED:
            outcome = "refunded"
            reply = "退款已执行，工单已按已退款关闭"
        elif operation.status == OperationStatus.REJECTED:
            outcome = "rejected"
            reply = "审批拒绝：未执行退款，工单已按拒绝关闭"
        else:
            outcome = "failed"
            reply = "外部执行已确认失败，工单保持开放并转人工处理"
        settled_state = {
            **state,
            "outcome": outcome,
            "next_action": "finished",
            "error_code": None,
            "reply": reply,
            "audit_event_ids": list(state.get("audit_event_ids") or []) + ids,
        }
        self.graph.update_state(cfg, settled_state, as_node="apply_decision")
        return self._build_result(tenant_id, thread_id, settled_state, ())

    def _loop_stop(self, tenant_id: str, thread_id: str, exc: Exception,
                   cfg: dict) -> RunResult:
        """死循环保护触发后的安全收口（确定性；触发后无新增领域写入、不执行退款）。

        - 触发时被拦截的节点体**不会执行**（异常在节点体调用前抛出），因此不产生新的
          领域写操作、审计事件或退款副作用；检测前已形成的草稿与审批事实原样保留
          （它们存在于领域事实源，本方法不触碰）；
        - 终态标记写入**持久 checkpoint**（只写流程状态，不写业务事实），
          使进程重启后的新实例仍能读到 AGENT_LOOP_DETECTED / escalated / 人工接管原因；
        - 写回失败时降级为本次返回可见（并记 structured 日志），不伪造成功。
        """
        reply = (f"检测到工作流反复执行超过上限（最多 {self._max_steps} 步），已安全停止并转人工处理；"
                 f"触发后未新增任何领域写入、未执行退款（错误码 {LOOP_ERROR_CODE}）")
        marker = {
            "outcome": "escalated",
            "error_code": LOOP_ERROR_CODE,
            "next_action": "escalated",
            "reply": reply,
        }
        # 读回当前 checkpoint 视图（只读），让返回的 state 仍带上 operation_id /
        # ticket_id / step_count / evidence_refs / audit_event_ids 等既有上下文。
        snapshot: dict = {}
        try:
            snapshot = dict(self.graph.get_state(cfg).values or {})
        except Exception as err:  # noqa: BLE001  读回失败不影响安全停止结论
            logger.warning("agent_loop_state_read_failed tenant=%s thread=%s error=%r",
                           tenant_id, thread_id, err)
        persisted = False
        try:
            # 以 escalate 节点身份写入终态标记：只写流程状态，不写业务事实。
            self.graph.update_state(cfg, marker, as_node="escalate")
            after = dict(self.graph.get_state(cfg).values or {})
            persisted = after.get("error_code") == LOOP_ERROR_CODE
        except Exception as err:  # noqa: BLE001  标记失败不影响安全停止结论
            logger.warning("agent_loop_marker_write_failed tenant=%s thread=%s error=%r",
                           tenant_id, thread_id, err)
        logger.error("agent_loop_safe_stop tenant=%s thread=%s error=%s max_steps=%d "
                     "persisted=%s blocked_node_not_executed=true new_domain_writes=0 "
                     "refund_executed=false",
                     tenant_id, thread_id, type(exc).__name__, self._max_steps, persisted)
        state = {**snapshot, **marker, "step_count": int(snapshot.get("step_count") or 0),
                 "thread_id": thread_id, "tenant_id": tenant_id}
        op_id = state.get("operation_id")
        if op_id:
            self._op_tenants[op_id] = tenant_id
        self._remember(tenant_id, thread_id)
        return RunResult(thread_id=thread_id, finished=True, outcome="escalated",
                         error_code=LOOP_ERROR_CODE, reply=reply, state=state,
                         audit_event_ids=list(state.get("audit_event_ids") or []))

    def _peek_thread(self, cfg: dict):
        """查看线程当前 checkpoint（不存在返回 None；存在返回 (values, interrupts)）。"""
        snap = self.graph.get_state(cfg)
        values = dict(snap.values or {})
        interrupts = tuple(getattr(snap, "interrupts", None) or ())
        if not values:
            return None
        return values, interrupts

    def _cfg(self, thread_id: str, tenant_id: Optional[str] = None) -> dict:
        """构造 LangGraph 配置（兼容入口：租户缺省时取本进程缓存）。"""
        tenant = tenant_id or self._recent_tenant.get(thread_id)
        if tenant is None:
            raise UnknownThreadError(self._unknown_message(thread_id))
        return self._cfg_for(tenant, thread_id)

    def _cfg_for(self, tenant_id: str, thread_id: str) -> dict:
        """Build the versioned checkpoint key for ``(tenant_id, thread_id)``.

        Existing pre-V1.2 checkpoints are accepted only when their stored
        ``tenant_id`` and ``thread_id`` exactly match the requested pair. This
        prevents the old delimiter collision from becoming a fallback leak.
        """
        key = _checkpoint_thread_key(tenant_id, thread_id)
        cfg = {"configurable": {"thread_id": key},
               "recursion_limit": self._recursion_limit}
        try:
            current = self.graph.get_state(cfg)
            if current.values:
                return cfg
            legacy_cfg = {
                "configurable": {"thread_id": _legacy_checkpoint_thread_key(tenant_id, thread_id)},
                "recursion_limit": self._recursion_limit,
            }
            legacy = self.graph.get_state(legacy_cfg)
            values = dict(legacy.values or {})
            if (values.get("tenant_id") == tenant_id
                    and values.get("thread_id") == thread_id):
                return legacy_cfg
        except Exception:  # noqa: BLE001 - checkpoint probe is fail-closed
            return cfg
        return cfg

    def _finalize(self, tenant_id: str, thread_id: str, raw: dict) -> RunResult:
        interrupts = raw.get("__interrupt__") or ()
        values = {k: v for k, v in raw.items() if k != "__interrupt__"}
        return self._build_result(tenant_id, thread_id, values, interrupts)

    def _build_result(self, tenant_id: str, thread_id: str, values: dict,
                      interrupts) -> RunResult:
        iv = None
        waiting_approval = False
        waiting_clarify = False
        if interrupts:
            first = interrupts[0]
            iv = dict(first.value) if getattr(first, "value", None) else {"type": "unknown"}
            waiting_approval = iv.get("type") == APPROVAL_INTERRUPT_TYPE
            waiting_clarify = iv.get("type") == CLARIFY_INTERRUPT_TYPE
        # 死循环安全停止是**持久终态**（标记已写入 checkpoint）：即使 checkpoint 里
        # 仍残留未决 interrupt，也不再对外表现为「等待中」，避免误导调用方继续 resume
        # 一个已安全停止的线程。判定只依赖 checkpoint 中的 error_code（不依赖内存状态）。
        loop_stopped = values.get("error_code") == LOOP_ERROR_CODE
        if loop_stopped:
            iv = None
            waiting_approval = False
            waiting_clarify = False
        # 登记本运行器见过的操作 → 租户（供 submit_decision/query/reconcile 无参回退）
        op_id = values.get("operation_id")
        if tenant_id and op_id:
            self._op_tenants[op_id] = tenant_id
        return RunResult(
            thread_id=thread_id,
            finished=loop_stopped or not interrupts,
            waiting_approval=waiting_approval,
            waiting_clarify=waiting_clarify,
            interrupt_value=iv,
            state=values,
            outcome=values.get("outcome"),
            error_code=values.get("error_code"),
            reply=values.get("reply"),
            audit_event_ids=list(values.get("audit_event_ids") or []),
        )
