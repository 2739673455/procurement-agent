"""请求追踪上下文，覆盖普通响应、异常响应和完整 SSE 生命周期。"""

import re
from time import monotonic
from uuid import uuid4

from loguru import logger
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors.exc_handlers import unhandled_exception_handler
from app.observability import context


def _identifier(value: str | None) -> str:
    """只接受长度受限的可打印关联标识，不将其用于身份认证。"""
    return (
        value
        if value and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value)
        else str(uuid4())
    )


class TraceMiddleware:
    """使用原生 ASGI 中间件，避免缓冲流式响应。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        request_id = _identifier(headers.get("X-Request-ID"))
        trace_id = _identifier(headers.get("X-Trace-ID") or request_id)
        values = (
            (context.request_id_ctx, request_id),
            (context.trace_id_ctx, trace_id),
            (context.method_ctx, scope["method"]),
            (context.path_ctx, scope["path"]),
            (
                context.client_ip_ctx,
                scope["client"][0] if scope.get("client") else None,
            ),
            (context.user_id_ctx, None),
        )
        tokens = [(variable, variable.set(value)) for variable, value in values]
        started = False
        status = 500
        begin = monotonic()

        async def traced_send(message: Message) -> None:
            nonlocal started, status
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Trace-ID"] = trace_id
            await send(message)

        try:
            try:
                await self.app(scope, receive, traced_send)
            except Exception as exc:
                if started:
                    logger.opt(exception=exc).error("响应开始后发生异常")
                    raise
                response = unhandled_exception_handler(Request(scope), exc)
                await response(scope, receive, traced_send)
        finally:
            logger.info(
                "HTTP 请求结束",
                status=status,
                duration_ms=round((monotonic() - begin) * 1000, 2),
            )
            for variable, token in reversed(tokens):
                variable.reset(token)
