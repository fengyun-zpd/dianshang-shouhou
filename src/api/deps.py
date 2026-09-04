"""K5：认证身份解析（中间件注入）与依赖。

身份只由认证结果推导（X-Api-Key → 本地身份），请求体中的 tenant_id 不被信任；
中间件把解析出的身份放入 request.state.identity。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware

from src.domain.models import Role


@dataclass(frozen=True)
class ApiIdentity:
    principal: str
    tenant_id: str
    role: Role
    customer_id: Optional[str] = None


class ApiTokenRegistry:
    """模拟认证注册表（演示/测试；生产接真实 IdP）。"""

    def __init__(self) -> None:
        self._tokens: dict[str, ApiIdentity] = {}

    def register(self, token: str, identity: ApiIdentity) -> None:
        self._tokens[token] = identity

    def resolve(self, token: Optional[str]) -> Optional[ApiIdentity]:
        if not token:
            return None
        return self._tokens.get(token)


class AuthMiddleware(BaseHTTPMiddleware):
    """解析 X-Api-Key → ApiIdentity 存入 request.state.identity；无效则 401。"""

    def __init__(self, app, registry: ApiTokenRegistry):
        super().__init__(app)
        self._registry = registry

    async def dispatch(self, request: Request, call_next):
        token = request.headers.get("X-Api-Key")
        identity = self._registry.resolve(token)
        if identity is None:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"request_id": _rid(request),
                         "code": "UNAUTHENTICATED",
                         "message": "无效或缺失 X-Api-Key"},
            )
        request.state.identity = identity
        return await call_next(request)


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def get_identity(request: Request) -> ApiIdentity:
    """依赖：读取已认证身份。"""
    identity = getattr(request.state, "identity", None)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="未认证")
    return identity
