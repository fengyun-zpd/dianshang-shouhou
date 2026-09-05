"""Mule Agent Bridge（阶段 6 / ADR-003）：外部 Agent 网络适配器。

边界（ADR-003 约束）：
- 桥接层只负责：身份映射、租户注入、Schema 校验、超时、审计、断路；
- 核心业务仍由本地领域服务裁决；外部 Agent 不能借桥接层扩大本地工具权限；
- 动作白名单只含只读查询与"发起售后请求"（客服入口语义：AGENT 草稿 + 人工审批），
  不暴露任何审批/执行/状态放行能力（FORBIDDEN_ACTIONS 不可达，测试断言）；
- 外部身份未注册 / 动作越权 / 请求级租户与映射不一致 → 一律拒绝并审计。

MCP 作为跨服务协议属规划（见 docs/MULE_BRIDGE.md）；本模块为进程内协议适配实现。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable, Optional

from pydantic import BaseModel, ValidationError

from src.agents import WorkflowRunner
from src.domain.after_sales import AfterSalesError
from src.domain.after_sales.ports import AfterSalesApplicationPort
from src.platform.reliability import CircuitBreaker, CircuitOpenError
from src.domain.models import Role
from src.rag import PolicyStore

from .models import (
    BridgeAction,
    BridgeEnvelope,
    BridgeIdentity,
    BridgeLogEntry,
    INBOUND_SCHEMAS,
    OUTBOUND_SCHEMAS,
    IdentityRegistry,
)

Executor = Callable[["MuleAgentBridge", str, str, dict], BridgeEnvelope]
_POOL = ThreadPoolExecutor(max_workers=4)


class BridgeExecutionError(Exception):
    """桥接执行层结构化错误（错误码原样透出，不吞不改写）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class MuleAgentBridge:
    """把本系统只读能力与"发起售后请求"暴露给外部 Agent 网络的受控桥。"""

    def __init__(
        self,
        service: AfterSalesApplicationPort,
        identities: Optional[IdentityRegistry] = None,
        policy_store: Optional[PolicyStore] = None,
        breaker: Optional[CircuitBreaker] = None,
        timeout_seconds: float = 3.0,
        runner_factory: Optional[Callable[[AfterSalesApplicationPort], WorkflowRunner]] = None,
    ):
        # service：实现 AfterSalesApplicationPort 的后端（MemoryAdapter / PgCommandAdapter）。
        # 桥接层只读查询与售后请求均经该 Port（Gateway / WorkflowRunner 同源）。
        self._service = service
        self._identities = identities if identities is not None else IdentityRegistry()
        self._policy_store = policy_store if policy_store is not None else PolicyStore()
        self._breaker = breaker if breaker is not None else CircuitBreaker(
            failure_threshold=3, reset_timeout_seconds=1.0,
        )
        self._timeout = timeout_seconds
        self._runner_factory = runner_factory or (lambda backend: WorkflowRunner(backend))
        self._logs: list[BridgeLogEntry] = []

    # ---------- 对外入口 ----------

    def invoke(self, principal: str, action: str, payload: dict,
               trace_id: Optional[str] = None) -> BridgeEnvelope:
        """外部 Agent 调用入口：身份 → 白名单 → 租户注入 → Schema → 执行 → 出站校验 → 审计。"""
        started = time.monotonic()

        identity = self._identities.resolve(principal)
        if identity is None:
            return self._finish(principal, action, "", "AUTH_ERROR", started,
                                "外部身份未注册，拒绝访问", payload)

        try:
            act = BridgeAction(action)
        except ValueError:
            return self._finish(principal, action, identity.tenant_id, "BAD_ACTION",
                                started, f"未知动作：{action}", payload)

        if act not in identity.allowed_actions:
            return self._finish(principal, action, identity.tenant_id, "FORBIDDEN",
                                started, f"身份 {principal} 无权调用 {action}", payload)

        allowed_roles = {
            BridgeAction.query_order: {Role.AGENT, Role.APPROVER, Role.SYSTEM},
            BridgeAction.query_ticket: {Role.AGENT, Role.APPROVER, Role.SYSTEM},
            BridgeAction.list_customer_tickets: {Role.AGENT, Role.APPROVER, Role.SYSTEM},
            BridgeAction.retrieve_policy: {Role.AGENT, Role.APPROVER, Role.SYSTEM},
            BridgeAction.submit_after_sales_request: {Role.CUSTOMER, Role.AGENT},
        }
        if identity.local_role not in allowed_roles.get(act, set()):
            return self._finish(principal, action, identity.tenant_id, "FORBIDDEN", started,
                                f"本地角色 {identity.local_role.value} 不得调用 {action}", payload)

        # 租户注入：请求级 tenant_id 必须与身份映射一致（不一致直接拒绝，杜绝跨租户）
        tenant_id = identity.tenant_id
        explicit = payload.get("tenant_id")
        if explicit is not None and str(explicit) != tenant_id:
            return self._finish(principal, action, tenant_id, "TENANT_MISMATCH",
                                started, "请求 tenant_id 与身份映射不一致，已拒绝", payload)
        payload = {k: v for k, v in payload.items() if k != "tenant_id"}

        # Schema 校验（入站）
        schema = INBOUND_SCHEMAS.get(act)
        if schema is None:
            return self._finish(principal, action, tenant_id, "UNSUPPORTED",
                                started, "该动作无入站 Schema", payload)
        try:
            model = schema.model_validate(payload)
        except ValidationError as e:
            return self._finish(principal, action, tenant_id, "VALIDATION_ERROR",
                                started, f"入站参数校验失败：{e.errors()[:2]}", payload)

        # 熔断保护：open 时拒绝（fail-closed）
        try:
            envelope = self._breaker.call(
                lambda: self._execute(identity, act, model, trace_id),
                fallback=lambda: BridgeEnvelope(
                    action=act, ok=False, error_code="CIRCUIT_OPEN",
                    error_detail="桥接熔断打开（fail-closed），请稍后重试"),
            )
        except CircuitOpenError:
            envelope = BridgeEnvelope(action=act, ok=False, error_code="CIRCUIT_OPEN",
                                      error_detail="桥接熔断打开（fail-closed）")
        except BridgeExecutionError as e:
            envelope = BridgeEnvelope(action=act, ok=False, error_code=e.code,
                                      error_detail=e.message)

        self._logs.append(BridgeLogEntry(
            principal=principal, action=act.value, tenant_id=tenant_id,
            status="ok" if envelope.ok else f"error:{envelope.error_code}",
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            detail=envelope.error_detail or "",
        ))
        return envelope

    def audit_log(self) -> list[BridgeLogEntry]:
        return list(self._logs)

    # ---------- 内部执行 ----------

    def _execute(self, identity: BridgeIdentity, act: BridgeAction,
                 model: BaseModel, trace_id: Optional[str]) -> BridgeEnvelope:
        tenant = identity.tenant_id
        future = _POOL.submit(self._dispatch, tenant, act, model, trace_id)
        try:
            data = future.result(timeout=self._timeout)
        except FutureTimeout:
            future.cancel()  # 运行中的任务无法强杀；提交类动作结果按未知状态处理
            raise BridgeExecutionError(
                "BRIDGE_TIMEOUT",
                "桥接执行超时；若动作可能产生副作用，必须按原 operation_id 对账，禁止换键重试",
            )
        except BridgeExecutionError as e:
            return BridgeEnvelope(action=act, ok=False, error_code=e.code,
                                  error_detail=e.message)
        except AfterSalesError as e:
            return BridgeEnvelope(action=act, ok=False, error_code=e.code.value,
                                  error_detail=e.message)
        except Exception as e:  # noqa: BLE001
            return BridgeEnvelope(action=act, ok=False, error_code="EXECUTION_ERROR",
                                  error_detail=repr(e)[:300])

        out_schema = OUTBOUND_SCHEMAS[act]
        try:
            out_schema.model_validate(data)  # 出站 Schema 校验
        except ValidationError as e:
            return BridgeEnvelope(action=act, ok=False, error_code="OUTPUT_SCHEMA_ERROR",
                                  error_detail=f"出站校验失败：{e.errors()[:2]}")
        return BridgeEnvelope(action=act, ok=True, data=data)

    def _dispatch(self, tenant: str, act: BridgeAction, model: BaseModel,
                  trace_id: Optional[str]):
        """动作 → 本地执行（只读查询或发起售后请求）。"""
        if act == BridgeAction.query_order:
            from src.agents.ports import AfterSalesGateway
            order = AfterSalesGateway(self._service).get_order(tenant, model.order_id)
            return {
                "order_id": order.order_id, "tenant_id": order.tenant_id,
                "customer_id": order.customer_id, "status": order.status.value,
                "paid_amount": str(order.paid_amount),
                "days_since_sign": order.days_since_sign,
            }
        if act == BridgeAction.query_ticket:
            from src.agents.ports import AfterSalesGateway
            ticket = AfterSalesGateway(self._service).get_ticket_for(tenant, model.ticket_id)
            return {
                "ticket_id": ticket.ticket_id, "tenant_id": ticket.tenant_id,
                "order_id": ticket.order_id, "customer_id": ticket.customer_id,
                "request_type": ticket.request_type.value, "status": ticket.status.value,
                "resolution": ticket.resolution,
            }
        if act == BridgeAction.list_customer_tickets:
            from src.agents.ports import AfterSalesGateway
            tickets = AfterSalesGateway(self._service).history_tickets(tenant, model.customer_id)
            return {
                "customer_id": model.customer_id, "count": len(tickets),
                "ticket_ids": [t.ticket_id for t in tickets],
            }
        if act == BridgeAction.retrieve_policy:
            from src.rag import InjectionDetected
            try:
                results = self._policy_store.search(tenant, model.query, top_k=model.top_k)
            except InjectionDetected as e:
                raise BridgeExecutionError("INJECTION_DETECTED",
                                           f"检索查询含提示注入，拒绝返回政策内容：{e}")
            return {
                "query": model.query, "has_evidence": bool(results),
                "results": [
                    {"citation": r.citation(), "policy_id": r.chunk.policy_id,
                     "text": r.chunk.text, "score": r.score,
                     "keyword_hits": r.keyword_hits}
                    for r in results
                ],
            }
        if act == BridgeAction.submit_after_sales_request:
            runner = self._runner_factory(self._service)
            result = runner.start(tenant, model.request_text,
                                  thread_id=model.thread_hint)
            st = result.state or {}
            return {
                "thread_id": result.thread_id,
                "waiting_clarify": result.waiting_clarify,
                "waiting_approval": result.waiting_approval,
                "ticket_id": st.get("ticket_id"),
                "operation_id": st.get("operation_id"),
                "outcome": result.outcome,
                "message": (result.reply or
                            ("已进入人工审批，等待本地授权人员决定（桥接层无审批权）"
                             if result.waiting_approval else
                             "已受理，等待澄清或转人工")),
            }
        raise BridgeExecutionError("UNSUPPORTED_ACTION", "不支持的桥接动作")

    # ---------- 内部 ----------

    def _finish(self, principal: str, action: str, tenant: str, code: str,
                started: float, detail: str, payload: dict) -> BridgeEnvelope:
        try:
            act = BridgeAction(action)
        except ValueError:
            act = BridgeAction.query_order  # 占位（仅用于日志/封装类型）
        self._logs.append(BridgeLogEntry(
            principal=principal, action=action, tenant_id=tenant,
            status=f"error:{code}", duration_ms=round((time.monotonic() - started) * 1000, 2),
            detail=detail,
        ))
        return BridgeEnvelope(action=act, ok=False, error_code=code, error_detail=detail)
