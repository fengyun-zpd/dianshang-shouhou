"""K5：FastAPI 接入层（路由不复制领域逻辑，仅适配/编排领域命令）。

- 身份由认证（AuthMiddleware）推导；请求体不带 tenant_id；
- 角色门禁：agent/customer 可创建工单与草稿、submit；approver 可审批/拒绝；
  system 可执行与对账；任何人不得借 API 伪造权限（错误码不被改写）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from starlette.middleware.base import BaseHTTPMiddleware

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    AfterSalesService,
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
from src.domain.after_sales.models import OperationStatus

from .deps import ApiIdentity, AuthMiddleware, TokenResolver, get_identity
from .errors import register_error_handlers
from .schemas import (
    AuditItemOut,
    DecisionIn,
    ErrorOut,
    ExecuteIn,
    OperationOut,
    ReconcileIn,
    RefundDraftIn,
    TicketCreateIn,
    TicketOut,
)

router = APIRouter(prefix="/api", tags=["after-sales"])


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response


def _service(request: Request) -> AfterSalesService:
    return request.app.state.service


def _require_role(identity: ApiIdentity, allowed: tuple[Role, ...]) -> None:
    if identity.role not in allowed:
        raise AfterSalesError(AfterSalesErrorCode.PERMISSION_DENIED,
                              f"角色 {identity.role.value} 无权执行该操作")


def _operation_out(request: Request, tenant: str, op) -> OperationOut:
    # 通过租户作用域读取以杜绝跨租户（只读辅助）
    svc = _service(request)
    op = svc.get_operation(op.operation_id)
    if op.tenant_id != tenant:
        raise AfterSalesError(AfterSalesErrorCode.TENANT_MISMATCH, "操作不属于当前租户")
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
    svc = _service(request)
    ticket = svc.create_ticket(CreateTicketCommand(
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
    svc = _service(request)
    from src.agents import AfterSalesGateway
    ticket = AfterSalesGateway(svc).get_ticket_for(identity.tenant_id, ticket_id)
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
    svc = _service(request)
    from src.agents import AfterSalesGateway
    AfterSalesGateway(svc).get_ticket_for(identity.tenant_id, ticket_id)  # 租户门禁
    return [_operation_out(request, identity.tenant_id, op)
            for op in svc.operations_of(ticket_id)]


# ---------- 动作草稿 ----------

@router.post("/tickets/{ticket_id}/refund-drafts", response_model=OperationOut,
             status_code=201)
def create_refund_draft(ticket_id: str, body: RefundDraftIn, request: Request,
                        identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.AGENT,))
    svc = _service(request)
    from src.agents import AfterSalesGateway
    AfterSalesGateway(svc).get_ticket_for(identity.tenant_id, ticket_id)  # 租户门禁
    op = svc.create_refund(CreateRefundCommand(
        ticket_id=ticket_id, amount=Decimal(body.amount),
        reason_detail=body.reason_detail, actor=Role.AGENT,
        idempotency_key=body.idempotency_key,
    ))
    return _operation_out(request, identity.tenant_id, op)


@router.post("/operations/{operation_id}/submit", response_model=OperationOut)
def submit(operation_id: str, request: Request,
           identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.AGENT,))
    svc = _service(request)
    op = svc.get_operation(operation_id)
    if op.tenant_id != identity.tenant_id:
        raise AfterSalesError(AfterSalesErrorCode.TENANT_MISMATCH, "操作不属于当前租户")
    op = svc.submit(SubmitCommand(operation_id=operation_id, actor=Role.AGENT))
    return _operation_out(request, identity.tenant_id, op)


# ---------- 审批 / 执行 / 对账 ----------

@router.post("/operations/{operation_id}/approve", response_model=OperationOut)
def approve(operation_id: str, body: DecisionIn, request: Request,
            identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.APPROVER,))
    svc = _service(request)
    op = svc.get_operation(operation_id)
    if op.tenant_id != identity.tenant_id:
        raise AfterSalesError(AfterSalesErrorCode.TENANT_MISMATCH, "操作不属于当前租户")
    expected = body.expected_version if body.expected_version is not None else op.version
    op = svc.approve(ApproveCommand(operation_id=operation_id, actor=Role.APPROVER,
                                    decision_version=expected))
    return _operation_out(request, identity.tenant_id, op)


@router.post("/operations/{operation_id}/reject", response_model=OperationOut)
def reject(operation_id: str, body: DecisionIn, request: Request,
           identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.APPROVER,))
    svc = _service(request)
    op = svc.get_operation(operation_id)
    if op.tenant_id != identity.tenant_id:
        raise AfterSalesError(AfterSalesErrorCode.TENANT_MISMATCH, "操作不属于当前租户")
    expected = body.expected_version if body.expected_version is not None else op.version
    op = svc.reject(RejectCommand(operation_id=operation_id, actor=Role.APPROVER,
                                  reason=body.reason or "审批人拒绝",
                                  decision_version=expected))
    return _operation_out(request, identity.tenant_id, op)


@router.post("/operations/{operation_id}/execute", response_model=OperationOut)
def execute(operation_id: str, body: ExecuteIn, request: Request,
            identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.SYSTEM,))  # 仅内部自动化（模拟外部执行）
    svc = _service(request)
    op = svc.get_operation(operation_id)
    if op.tenant_id != identity.tenant_id:
        raise AfterSalesError(AfterSalesErrorCode.TENANT_MISMATCH, "操作不属于当前租户")
    op = svc.execute(ExecuteCommand(operation_id=operation_id, actor=Role.SYSTEM,
                                    external_result=body.external_result))
    return _operation_out(request, identity.tenant_id, op)


@router.post("/operations/{operation_id}/reconcile", response_model=OperationOut)
def reconcile(operation_id: str, body: ReconcileIn, request: Request,
              identity: ApiIdentity = Depends(get_identity)):
    _require_role(identity, (Role.SYSTEM,))  # unknown 只能原操作对账收口
    svc = _service(request)
    op = svc.get_operation(operation_id)
    if op.tenant_id != identity.tenant_id:
        raise AfterSalesError(AfterSalesErrorCode.TENANT_MISMATCH, "操作不属于当前租户")
    op = svc.reconcile(ReconcileCommand(operation_id=operation_id, actor=Role.SYSTEM,
                                        result=body.result))
    return _operation_out(request, identity.tenant_id, op)


# ---------- 审计 ----------

@router.get("/audit", response_model=list[AuditItemOut])
def audit(request: Request,
          identity: ApiIdentity = Depends(get_identity),
          entity_type: Optional[str] = None,
          entity_id: Optional[str] = None):
    _require_role(identity, (Role.AGENT, Role.APPROVER, Role.SYSTEM))
    svc = _service(request)
    items = []
    for e in svc.audit_log():
        if entity_type and e.entity_type != entity_type:
            continue
        if entity_id and e.entity_id != entity_id:
            continue
        # 租户作用域：审计事件实体须属于当前租户
        if svc.entity_tenant(e.entity_type, e.entity_id) != identity.tenant_id:
            continue
        items.append(AuditItemOut(action=e.action, entity_type=e.entity_type,
                                  entity_id=e.entity_id, actor=e.actor.value,
                                  before=e.before, after=e.after, note=e.note))
    return items


# ---------- 工厂 ----------

def create_app(service: AfterSalesService, registry: TokenResolver,
               pg_probe=None) -> FastAPI:
    """FastAPI 工厂。pg_probe：可调用 → bool，用于 /health/ready 反映 PostgreSQL 可用性
    （None=未配置外部依赖探测，ready 恒 ok）。业务命令层默认仍为注入的 service
    （内存 AfterSalesService 或未来 PG-first 装配），本工厂不连接任何真实外部系统。"""
    app = FastAPI(title="OpsPilot After-Sales API", version="0.1")
    app.state.service = service
    app.state.registry = registry
    app.state.pg_probe = pg_probe
    app.add_middleware(AuthMiddleware, registry=registry)
    app.add_middleware(RequestIdMiddleware)

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
    register_error_handlers(app)
    return app
