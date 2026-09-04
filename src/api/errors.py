"""K5：结构化错误映射（领域错误码不被改写；request_id 随错误返回）。"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.domain.after_sales import AfterSalesError
from src.domain.after_sales.models import AfterSalesErrorCode

_NOT_FOUND = {
    AfterSalesErrorCode.TICKET_NOT_FOUND,
    AfterSalesErrorCode.OPERATION_NOT_FOUND,
    AfterSalesErrorCode.ORDER_NOT_FOUND,
}
_FORBIDDEN = {
    AfterSalesErrorCode.PERMISSION_DENIED,
    AfterSalesErrorCode.TENANT_MISMATCH,
}
_CONFLICT = {
    AfterSalesErrorCode.IDEMPOTENCY_CONFLICT,
    AfterSalesErrorCode.OPERATION_UNKNOWN_CONFLICT,
    AfterSalesErrorCode.INVALID_STATE_TRANSITION,
    AfterSalesErrorCode.DECISION_VERSION_MISMATCH,
    AfterSalesErrorCode.TICKET_HAS_OPEN_OPERATIONS,
}


def _http_status(code: AfterSalesErrorCode) -> int:
    if code in _NOT_FOUND:
        return 404
    if code in _FORBIDDEN:
        return 403
    if code in _CONFLICT:
        return 409
    return 400


def _body(request: Request, code: str, message: str) -> dict:
    return {"request_id": getattr(request.state, "request_id", "unknown"),
            "code": code, "message": message}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AfterSalesError)
    async def domain_error_handler(request: Request, exc: AfterSalesError):
        return JSONResponse(status_code=_http_status(exc.code),
                            content=_body(request, exc.code.value, exc.message))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422,
                            content=_body(request, "VALIDATION_ERROR",
                                          str(exc.errors()[:3])))

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(status_code=exc.status_code,
                            content=_body(request, f"HTTP_{exc.status_code}", detail))

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        return JSONResponse(status_code=500,
                            content=_body(request, "INTERNAL_ERROR", "服务器内部错误"))
