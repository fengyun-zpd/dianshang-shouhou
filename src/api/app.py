"""K5：FastAPI 接入层（路由不复制领域逻辑，仅适配/编排领域命令）。

- 身份由认证（AuthMiddleware）推导；请求体不带 tenant_id；
- 本层唯一依赖 AfterSalesApplicationPort（MemoryAdapter/PgCommandAdapter 均可，
  不 isinstance 分支）；所有领域调用一律 tenant-first；
- 角色门禁：agent/customer 可创建工单与草稿、submit；approver 可审批/拒绝；
  system 可执行与对账；任何人不得借 API 伪造权限（错误码不被改写）。
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
from .errors import register_error_handlers
from .schemas import (
    AgentLabBoundaryScenarioIn,
    AgentLabRetrieveIn,
    AgentLabTraceIn,
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


# ---------- 工厂 ----------

def create_app(service: AfterSalesApplicationPort, registry: TokenResolver,
               pg_probe=None, require_expected_version: bool = False,
               demo_reset=None) -> FastAPI:
    """FastAPI 工厂。service 为实现 AfterSalesApplicationPort 的后端（Memory/PgCommand
    Adapter 均可，调用方不 isinstance）。require_expected_version=True（pg profile）：
    审批/拒绝必须由客户端提交 expected_version，服务端不代填（422）。
    pg_probe：可调用 → bool，用于 /health/ready 反映 PostgreSQL 可用性。"""
    app = FastAPI(title="OpsPilot After-Sales API", version="0.1")
    app.state.service = service
    app.state.registry = registry
    app.state.pg_probe = pg_probe
    app.state.require_expected_version = require_expected_version
    app.state.demo_reset = demo_reset
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

    @app.post("/api/demo/reset", tags=["demo"])
    def reset_demo(identity: ApiIdentity = Depends(get_identity)):
        """重置固定合成演示数据；仅 memory demo 注册，生产/PG profile 不暴露。"""
        _require_role(identity, (Role.AGENT,))
        reset = app.state.demo_reset
        if reset is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="演示重置仅适用于 memory 合成后端")
        reset()
        return {"status": "reset", "mode": "memory_demo", "side_effect": False}

    register_error_handlers(app)
    return app
