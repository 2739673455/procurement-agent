"""经过身份验证的会话操作接口。"""

import asyncio
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.api.messages import public_messages
from app.config import app_config
from app.errors import AgentError
from app.integrations.erpnext import ERPNext

router = APIRouter()


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sid: SecretStr
    action: Literal[
        "list", "create", "messages", "rename", "delete", "send", "subscribe", "stop"
    ]
    conversation_id: UUID | None = None
    message: str = Field(default="", max_length=4000)
    title: str = Field(default="", max_length=64)


@router.post("/conversations")
async def conversations(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 20000:
            raise AgentError("请求过大。", 413)
    try:
        command = Command.model_validate_json(bytes(body))
    except ValidationError:
        raise AgentError("请求格式无效。", 422) from None
    erp = ERPNext(command.sid.get_secret_value())
    user = await asyncio.to_thread(erp.authenticate)
    owner = (app_config.cfg.erpnext.site, user)
    manager = request.app.state.manager
    async with manager.lock:
        repository = manager.repository
        if command.action == "list":
            return {"conversations": await repository.list(owner)}
        if command.action == "create":
            return {"id": await repository.create(owner)}
        if command.conversation_id is None:
            raise AgentError("缺少会话 ID。")
        identifier = str(command.conversation_id)
        await repository.require(owner, identifier)
        match command.action:
            case "messages":
                return {
                    "messages": public_messages(await manager.messages(identifier)),
                    "running": identifier in manager.runs,
                }
            case "rename":
                if not command.title.strip():
                    raise AgentError("标题不能为空。")
                await repository.rename(identifier, command.title.strip())
            case "delete":
                await manager.stop(identifier)
                await manager.checkpointer.adelete_thread(identifier)
                await repository.delete(identifier)
            case "stop":
                await manager.stop(identifier)
            case "send" | "subscribe":
                if command.action == "send":
                    if not command.message.strip():
                        raise AgentError("消息不能为空。")
                    events = manager.start(identifier, erp, command.message.strip())
                else:
                    events = manager.subscribe(identifier)
                return StreamingResponse(
                    events,
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
        return {"ok": True}
