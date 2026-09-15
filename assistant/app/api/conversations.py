"""会话 HTTP 接口：请求解析、依赖注入及 JSON/SSE 响应。"""

import asyncio
import json
from contextlib import suppress
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.dependencies import AuthenticatedUser, authenticate, conversation_service
from app.contracts.conversations import Command
from app.services.conversations import ConversationService

router = APIRouter()
SSE_HEARTBEAT_SECONDS = 10


async def encode_sse(events):
    """编码业务事件并发送心跳，连接结束时只关闭当前订阅。"""
    pending = None
    try:
        pending = asyncio.ensure_future(anext(events))
        while True:
            done, _ = await asyncio.wait({pending}, timeout=SSE_HEARTBEAT_SECONDS)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                break
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            pending = asyncio.ensure_future(anext(events))
    finally:
        try:
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await pending
        finally:
            await events.aclose()


@router.post("/conversations")
async def conversations(
    command: Command,
    user: Annotated[AuthenticatedUser, Depends(authenticate)],
    service: Annotated[ConversationService, Depends(conversation_service)],
):
    result = await service.execute(command, user.owner, user.erp)
    if command.action in ("send", "subscribe"):
        return StreamingResponse(
            encode_sse(result),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return result
