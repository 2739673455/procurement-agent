"""会话 HTTP 接口：请求解析、依赖注入及 JSON/SSE 响应。"""

from collections.abc import Mapping
from typing import Annotated

from agentscope.app._router._schema import ChatTriggerResponse
from fastapi import APIRouter, Depends
from starlette.responses import StreamingResponse

from app.api.dependencies import AuthenticatedUser, authenticate, session_service
from app.contracts.sessions import Command
from app.services.sessions import SessionService

router = APIRouter()


@router.post("/sessions", response_model=None)
async def sessions(
    command: Command,
    user: Annotated[AuthenticatedUser, Depends(authenticate)],
    service: Annotated[SessionService, Depends(session_service)],
) -> Mapping[str, object] | ChatTriggerResponse | StreamingResponse:
    """分发已认证的会话操作；订阅返回框架 SSE 响应，其余操作返回 JSON。"""
    return await service.execute(command, user.owner, user.erp)
