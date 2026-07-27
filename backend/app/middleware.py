import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware

from .services.auth_rate_limit import client_ip, consume_auth_attempt


logger = logging.getLogger(__name__)


class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "POST" and request.url.path in {
            "/api/auth/register",
            "/api/auth/login",
        }:
            address = client_ip(
                request.client.host if request.client else None,
                request.headers.get("x-forwarded-for"),
            )
            try:
                decision = consume_auth_attempt(address)
            except SQLAlchemyError:
                logger.exception("Authentication rate limiter is unavailable")
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": "认证服务暂时不可用，请稍后重试。",
                        "code": "AUTH_RATE_LIMIT_UNAVAILABLE",
                    },
                )
            if not decision.allowed:
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(decision.retry_after)},
                    content={
                        "detail": f"操作过于频繁，请在 {decision.retry_after} 秒后重试。",
                        "code": "AUTH_RATE_LIMITED",
                        "retry_after": decision.retry_after,
                    },
                )
        return await call_next(request)
