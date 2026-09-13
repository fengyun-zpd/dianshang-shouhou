"""K5：FastAPI 接入层（路由不复制领域逻辑，仅适配/编排领域命令）。

- 身份由认证（AuthMiddleware）推导；请求体不带 tenant_id；
- 本层唯一依赖 AfterSalesApplicationPort（MemoryAdapter/PgCommandAdapter 均可，
  不 isinstance 分支）；所有领域调用一律 tenant-first；
- 角色门禁：agent/customer 可创建工单与草稿、submit；approver 可审批/拒绝；
  system 可执行与对账；任何人不得借 API 伪造权限（错误码不被改写）；
- Agent 生命周期（`/api/v1/agent/*`）：只转发给已装配的 WorkflowRunner（构造参数
  agent_runner），不复制任何业务逻辑；decision 不携带审批结论（审批先写入领域事实源）；
  未装配运行器 → 503，不静默降级。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    ApproveCommand,
    CreateRefundCommand,
    CreateTicketCommand,
    ExecuteCommand,
    ReconcileCommand,
    RejectCommand,
    RequestType,
    Role,
    SubmitCommand,
)
from src.domain.after_sales.ports import AfterSalesApplicationPort

from .deps import ApiIdentity, AuthMiddleware, TokenResolver, get_identity
from .errors import AgentStateError, register_error_handlers
from .schemas import (
    AgentClarifyIn,
    AgentDecisionIn,
    AgentLabBoundaryScenarioIn,
    AgentLabRetrieveIn,
    AgentLabTraceIn,
    AgentStartIn,
    AuditItemOut,
    DecisionIn,
    ExecuteIn,
    OperationOut,
    ReconcileIn,
    RefundDraftIn,
    TicketCreateIn,
    TicketOut,
)
from .agent_lab import retrieve_policy, run_boundary_scenario, run_trace

router = APIRouter(prefix="/api", tags=["after-sales"])
UI_DIR = Path(__file__).resolve().parent / "ui"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response


def _service(request: Request) -> AfterSalesApplicationPort:
    return request.app.state.service


def _require_role(identity: ApiIdentity, allowed: tuple[Role, ...]) -> None:
    if identity.role not in allowed:
        raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED,
                              f"角色 {identity.role.value} 无权执行该操作")


def _operation_out(request: Request, tenant: str, op) -> OperationOut:
    # 通过租户作用域读取以杜绝跨租户（只读辅助；后端自带租户门禁）
    port = _service(request)
    op = port.get_operation(tenant, op.operation_id)
    return OperationOut(
        operation_id=op.operation_id, ticket_id=op.ticket_id, order_id=op.order_id,
        op_type=op.op_type.value,
        amount=str(op.amount) if op.amount is not None else None,
        status=op.status.value, idempotency_key=op.idempotency_key,
        version=op.version, decision_version=op.decision_version, executed=op.executed,
    )


# ---------- 工单 ----------

@router.post("/tickets", response_model=TicketOut, status_code=201)
def create_ticket(body: TicketCreateIn, request: Request,
                  identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.AGENT, Role.CUSTOMER))
    actor = Role.AGENT if identity.role == Role.AGENT else Role.CUSTOMER
    if actor == Role.CUSTOMER and body.customer_id != identity.customer_id:
        raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED,
                              "客户只能为自己的订单创建工单")
    port = _service(request)
    ticket = port.create_ticket(CreateTicketCommand(
        tenant_id=identity.tenant_id, order_id=body.order_id,
        customer_id=body.customer_id,
        request_type=RequestType(body.request_type),
        reason=body.reason, reason_tags=tuple(body.reason_tags),
        actor=actor, idempotency_key=body.idempotency_key,
    ))
    return TicketOut(ticket_id=ticket.ticket_id, tenant_id=ticket.tenant_id,
                     order_id=ticket.order_id, customer_id=ticket.customer_id,
                     request_type=ticket.request_type.value, reason=ticket.reason,
                     status=ticket.status.value, resolution=ticket.resolution)


@router.get("/tickets/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: str, request: Request,
               identity: ApiIdentity = Depends(get_identity)):
    port = _service(request)
    # tenant-first 只读：后端自带租户门禁（不存在→404 / 跨租户→403）
    ticket = port.get_ticket(identity.tenant_id, ticket_id)
    # 客户级资源授权（D3）：CUSTOMER 只能读取自己的工单；
    # 越权读取返回 403（PERMISSION_DENIED），响应不含目标工单任何字段
    if identity.role == Role.CUSTOMER and identity.customer_id != ticket.customer_id:
        raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED,
                              "客户只能访问自己的工单")
    return TicketOut(ticket_id=ticket.ticket_id, tenant_id=ticket.tenant_id,
                     order_id=ticket.order_id, customer_id=ticket.customer_id,
                     request_type=ticket.request_type.value, reason=ticket.reason,
                     status=ticket.status.value, resolution=ticket.resolution)


@router.get("/tickets/{ticket_id}/operations", response_model=list[OperationOut])
def list_operations(ticket_id: str, request: Request,
                    identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.AGENT, Role.APPROVER, Role.SYSTEM))
    port = _service(request)
    # list_operations 为 tenant-first（工单门禁在 Port/Adapter 内：不存在→404/跨租户→拒绝），
    # 无需在此重复裸查工单
    return [_operation_out(request, identity.tenant_id, op)
            for op in port.list_operations(identity.tenant_id, ticket_id)]


# ---------- 动作草稿 ----------

@router.post("/tickets/{ticket_id}/refund-drafts", response_model=OperationOut,
             status_code=201)
def create_refund_draft(ticket_id: str, body: RefundDraftIn, request: Request,
                        identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.AGENT,))
    port = _service(request)
    port.get_ticket(identity.tenant_id, ticket_id)  # 租户门禁（与既有 404/403 语义一致）
    op = port.create_refund_draft(identity.tenant_id, CreateRefundCommand(
        ticket_id=ticket_id, amount=Decimal(body.amount),
        reason_detail=body.reason_detail, actor=Role.AGENT,
        idempotency_key=body.idempotency_key,
    ))
    return _operation_out(request, identity.tenant_id, op)


@router.post("/operations/{operation_id}/submit", response_model=OperationOut)
def submit(operation_id: str, request: Request,
           identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.AGENT,))
    port = _service(request)
    port.get_operation(identity.tenant_id, operation_id)  # tenant-first 门禁
    op = port.submit(identity.tenant_id,
                     SubmitCommand(operation_id=operation_id, actor=Role.AGENT))
    return _operation_out(request, identity.tenant_id, op)


# ---------- 审批 / 执行 / 对账 ----------

def _decision_expected_version(request: Request, body, op_version: int) -> int:
    """审批/拒绝的 expected_version 解析。

    require_expected_version=True（pg profile）→ 客户端必填；缺失 → 422 稳定错误，
    服务端**禁止**用当前 op.version 兜底（防读-改-写竞态/误导）。memory/演示保留宽松兜底。
    """
    if body.expected_version is not None:
        return body.expected_version
    if getattr(request.app.state, "require_expected_version", False):
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(
            status_code=422,
            detail={"code": "EXPECTED_VERSION_REQUIRED",
                    "message": "pg profile：expected_version 必填，服务端不代填（防并发覆盖）"})
    return op_version


@router.post("/operations/{operation_id}/approve", response_model=OperationOut)
def approve(operation_id: str, body: DecisionIn, request: Request,
            identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.APPROVER,))
    port = _service(request)
    op = port.get_operation(identity.tenant_id, operation_id)  # tenant-first 门禁
    expected = _decision_expected_version(request, body, op.version)
    op = port.approve(identity.tenant_id,
                      ApproveCommand(operation_id=operation_id, actor=Role.APPROVER,
                                     decision_version=expected),
                      decided_by=identity.principal)
    return _operation_out(request, identity.tenant_id, op)


@router.post("/operations/{operation_id}/reject", response_model=OperationOut)
def reject(operation_id: str, body: DecisionIn, request: Request,
           identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.APPROVER,))
    port = _service(request)
    op = port.get_operation(identity.tenant_id, operation_id)  # tenant-first 门禁
    expected = _decision_expected_version(request, body, op.version)
    op = port.reject(identity.tenant_id,
                     RejectCommand(operation_id=operation_id, actor=Role.APPROVER,
                                   reason=body.reason or "审批人拒绝",
                                   decision_version=expected),
                     decided_by=identity.principal)
    return _operation_out(request, identity.tenant_id, op)


@router.post("/operations/{operation_id}/execute", response_model=OperationOut)
def execute(operation_id: str, body: ExecuteIn, request: Request,
            identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.SYSTEM,))  # 仅内部自动化（模拟外部执行）
    port = _service(request)
    port.get_operation(identity.tenant_id, operation_id)  # tenant-first 门禁
    op = port.execute(identity.tenant_id,
                      ExecuteCommand(operation_id=operation_id, actor=Role.SYSTEM,
                                     external_result=body.external_result))
    return _operation_out(request, identity.tenant_id, op)


@router.post("/operations/{operation_id}/reconcile", response_model=OperationOut)
def reconcile(operation_id: str, body: ReconcileIn, request: Request,
              identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.SYSTEM,))  # unknown 只能原操作对账收口
    port = _service(request)
    port.get_operation(identity.tenant_id, operation_id)  # tenant-first 门禁
    op = port.reconcile(identity.tenant_id,
                        ReconcileCommand(operation_id=operation_id, actor=Role.SYSTEM,
                                         result=body.result))
    return _operation_out(request, identity.tenant_id, op)


# ---------- 审计 ----------

@router.get("/audit", response_model=list[AuditItemOut])
def audit(request: Request,
          identity: ApiIdentity = Depends(get_identity),
          entity_type: Optional[str] = None,
          entity_id: Optional[str] = None):
    _require_role(identity, (Role.AGENT, Role.APPROVER, Role.SYSTEM))
    port = _service(request)
    items = []
    # 租户作用域审计：Port.audit_log(tenant_id) 已按租户过滤（内存按实体归属 / PG 按行租户）
    for e in port.audit_log(identity.tenant_id):
        if entity_type and e.entity_type != entity_type:
            continue
        if entity_id and e.entity_id != entity_id:
            continue
        items.append(AuditItemOut(action=e.action, entity_type=e.entity_type,
                                  entity_id=e.entity_id, actor=e.actor.value,
                                  before=e.before, after=e.after, note=e.note))
    return items


# ---------- Agent Lab（隔离合成沙箱，只读演示） ----------

@router.post("/agent-lab/trace")
def agent_lab_trace(body: AgentLabTraceIn,
                    identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.AGENT,))
    return run_trace(body.mode)


@router.post("/agent-lab/retrieve")
def agent_lab_retrieve(body: AgentLabRetrieveIn,
                       identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.AGENT,))
    return retrieve_policy(body.query)


@router.post("/agent-lab/boundary-scenario")
def agent_lab_boundary_scenario(body: AgentLabBoundaryScenarioIn,
                                identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.AGENT,))
    return run_boundary_scenario(body.name)


# ---------- Agent 生命周期（WorkflowRunner HTTP 主链路） ----------
#
# 设计边界：
# - 本层不复制任何业务逻辑：start / clarify / decision / state 全部转发给当前已装配的
#   WorkflowRunner（memory profile = 合成数据运行器；pg profile = 真实 PostgreSQL 运行器）；
# - 身份边界（V1 只服务内部坐席）：start/clarify 仅 AGENT；decision 仅 APPROVER/SYSTEM；
#   state 允许 AGENT/APPROVER/SYSTEM。**客户自助入口属规划能力，当前不开放**；
# - 租户一律取认证身份的 tenant_id，请求体不接受 tenant_id（携带 → 422）；
# - 外部执行结果不由 HTTP 调用者指定：只能由 SYSTEM 角色的领域执行接口
#   （/api/operations/{id}/execute）写入，再由 decision 重读领域事实；
# - /decision **不携带审批结论**：审批结果必须先由领域审批接口写入事实源
#   （/api/operations/{id}/approve|reject），本接口只触发 resume，apply_decision 重读事实。

def _agent_runner(request: Request):
    runner = getattr(request.app.state, "agent_runner", None)
    if runner is None:
        # 未装配运行器时明确 503，不静默降级到领域接口或伪造结果
        raise AgentStateError(503, "AGENT_RUNNER_UNAVAILABLE",
                              "Agent 运行器未装配：请以 run_api.py --backend memory|pg 启动")
    return runner


def _agent_view(result) -> dict:
    """统一结果结构（start / clarify / decision / state 共用）。"""
    state = dict(result.state or {})
    return {
        "thread_id": result.thread_id,
        "finished": result.finished,
        "waiting_approval": result.waiting_approval,
        "waiting_clarify": result.waiting_clarify,
        "interrupt": result.interrupt_value,
        "next_action": state.get("next_action"),
        "operation_id": state.get("operation_id"),
        "ticket_id": state.get("ticket_id"),
        "outcome": result.outcome,
        "error_code": result.error_code,
        "reply": result.reply,
        "evidence_refs": list(state.get("evidence_refs") or []),
        "audit_event_ids": list(result.audit_event_ids or []),
        "step_count": int(state.get("step_count") or 0),
        "checkpoint_note": ("checkpoint 只保存流程恢复状态；工单/操作/审批/执行等业务最终事实"
                            "仍从领域服务（PostgreSQL）读取"),
        "state": state,
    }


@router.post("/v1/agent/start")
def agent_start(body: AgentStartIn, request: Request,
                identity: ApiIdentity = Depends(get_identity)):
    """启动一轮售后请求（**仅内部坐席 AGENT**）。thread_id 缺省由运行器生成。"""
    _require_role(identity, (Role.AGENT,))
    result = _agent_runner(request).start(
        identity.tenant_id, body.message, thread_id=body.thread_id,
        order_id_hint=body.order_id_hint)
    return _agent_view(result)


@router.post("/v1/agent/{thread_id}/clarify")
def agent_clarify(thread_id: str, body: AgentClarifyIn, request: Request,
                  identity: ApiIdentity = Depends(get_identity)):
    """澄清补参：只允许在澄清中断状态调用，补参后回到原线程继续执行（仅 AGENT）。

    不允许借澄清接口修改租户、金额、审批状态、操作状态或权限（schema 已禁止额外字段）。
    """
    _require_role(identity, (Role.AGENT,))
    runner = _agent_runner(request)
    current = runner.get_state(thread_id, tenant_id=identity.tenant_id)
    if not current.waiting_clarify:
        raise AgentStateError(
            409, "AGENT_NOT_WAITING_CLARIFY",
            f"线程 {thread_id} 当前不在澄清状态（finished={current.finished}，"
            f"waiting_approval={current.waiting_approval}），拒绝澄清补参")
    payload: dict = {}
    if body.order_id:
        payload["order_id"] = body.order_id
    if body.description:
        payload["description"] = body.description
    elif body.message:
        payload["description"] = body.message
    result = runner.resume(thread_id, payload=payload, tenant_id=identity.tenant_id)
    return _agent_view(result)


@router.post("/v1/agent/{thread_id}/decision")
def agent_decision(thread_id: str, request: Request,
                   body: Optional[AgentDecisionIn] = None,
                   identity: ApiIdentity = Depends(get_identity)):
    """触发审批后的工作流恢复（只触发重读，不携带审批结论）。

    - 请求体允许为空 `{}`；携带 approved/rejected/decision 等字段 → 422；
    - 仅 APPROVER / SYSTEM 可调用；
    - 领域事实仍为待审批 → 工作流继续等待；approved → 执行；rejected → 拒绝收口；
      unknown → operation_unknown（不自动换键重试）。
    """
    _require_role(identity, (Role.APPROVER, Role.SYSTEM))
    runner = _agent_runner(request)
    current = runner.get_state(thread_id, tenant_id=identity.tenant_id)
    if not current.waiting_approval:
        raise AgentStateError(
            409, "AGENT_NOT_WAITING_APPROVAL",
            f"线程 {thread_id} 当前不在审批等待状态（finished={current.finished}）；"
            "审批结果请先通过领域审批接口写入事实源")
    # 只传占位恢复值：审批结论一律由 apply_decision 从领域事实源重读
    result = runner.resume(thread_id, payload="_continue_", tenant_id=identity.tenant_id)
    return _agent_view(result)


@router.get("/v1/agent/{thread_id}/state")
def agent_state(thread_id: str, request: Request,
                identity: ApiIdentity = Depends(get_identity)):
    """只读查询线程视图（内部坐席：AGENT / APPROVER / SYSTEM）。

    线程作用域为 (tenant_id, thread_id)；不存在或属于其他租户统一返回 404，
    不暴露所属租户。
    """
    _require_role(identity, (Role.AGENT, Role.APPROVER, Role.SYSTEM))
    return _agent_view(_agent_runner(request).get_state(
        thread_id, tenant_id=identity.tenant_id))


# ---------- 工厂 ----------

def create_app(service: AfterSalesApplicationPort, registry: TokenResolver,
               pg_probe=None, require_expected_version: bool = False,
               demo_reset=None, agent_runner=None) -> FastAPI:
    """FastAPI 工厂。service 为实现 AfterSalesApplicationPort 的后端（Memory/PgCommand
    Adapter 均可，调用方不 isinstance）。require_expected_version=True（pg profile）：
    审批/拒绝必须由客户端提交 expected_version，服务端不代填（422）。
    pg_probe：可调用 → bool，用于 /health/ready 反映 PostgreSQL 可用性。
    demo_reset：仅 memory 合成演示后端提供；提供时注册 /api/demo/reset，
    **pg profile（None）下该路由根本不注册**（不是注册后返回 404）。
    agent_runner：可选 WorkflowRunner——装配后 /api/v1/agent/* 生效；未装配时明确 503。
    """
    app = FastAPI(title="OpsPilot After-Sales API", version="0.1")
    app.state.service = service
    app.state.registry = registry
    app.state.pg_probe = pg_probe
    app.state.require_expected_version = require_expected_version
    app.state.demo_reset = demo_reset
    app.state.agent_runner = agent_runner
    app.add_middleware(AuthMiddleware, registry=registry)
    app.add_middleware(RequestIdMiddleware)

    # 中文工作台是本地演示入口；业务写操作仍全部走下方受认证保护的 /api 路由。
    app.mount("/assets", StaticFiles(directory=str(UI_DIR)), name="assets")

    @app.get("/", include_in_schema=False)
    def workspace():
        return FileResponse(UI_DIR / "index.html")

    @app.get("/health/live", tags=["ops"])
    def liveness():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["ops"])
    def readiness(request: Request):
        probe = app.state.pg_probe
        if probe is not None:
            try:
                ok = bool(probe())
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=503,
                    content={"status": "degraded",
                             "reason": "PostgreSQL 不可用（业务事实源依赖）"},
                )
        return {"status": "ok", "deps": {"postgresql": "ok" if probe is not None else "not-configured"}}

    app.include_router(router)

    if demo_reset is not None:
        @app.post("/api/demo/reset", tags=["demo"])
        def reset_demo(identity: ApiIdentity = Depends(get_identity)):
            """重置固定合成演示数据；仅 memory demo 注册。

            pg profile（demo_reset=None）下本路由**不注册**：生产/PG profile 不暴露
            memory reset，避免任何“重置业务库”的可能。
            """
            _require_role(identity, (Role.AGENT,))
            app.state.demo_reset()
            return {"status": "reset", "mode": "memory_demo", "side_effect": False}

    register_error_handlers(app)
    return app
