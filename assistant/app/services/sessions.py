"""将已认证的 ERPNext 请求适配为 Runtime 会话操作。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from uuid import UUID

from agentscope.app._router._schema import ChatTriggerResponse
from agentscope.app.storage import (
    SessionRecord,
)
from starlette.responses import StreamingResponse

from app.clients.erpnext.client import ERPNext
from app.contracts.sessions import (
    AttachmentInfo,
    AttachmentUpload,
    Command,
    PageContext,
)
from app.errors.agent import AgentError
from app.runtime import Runtime, SessionListItem, SessionSnapshot
from app.services.attachments import AttachmentService
from app.services.inputs import user_message


class SessionService:
    """将采购身份、页面和附件适配为 Runtime 公共输入。"""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime
        self._catalog = runtime.catalog
        self._attachments = AttachmentService(runtime.workspace_manager)
        self._lock = asyncio.Lock()

    def _user_id(self, owner: tuple[str, str]) -> str:
        """将已认证的站点和用户名编码为运行用户标识。"""
        return json.dumps(owner, ensure_ascii=False, separators=(",", ":"))

    async def execute(
        self, command: Command, owner: tuple[str, str], erp: ERPNext
    ) -> Mapping[str, object] | ChatTriggerResponse | StreamingResponse:
        """分发已认证请求，各操作分别检查会话归属和运行条件。"""
        match command.action:
            case "list":
                return await self.list(owner)
            case "create":
                return await self.create(owner)
            case "messages":
                return await self.messages(owner, command.session_id)
            case "rename":
                return await self.rename(owner, command.session_id, command.title)
            case "delete":
                return await self.delete(owner, command.session_id, erp)
            case "upload":
                return await self.upload(owner, command.session_id, command.upload)
            case "send":
                return await self.send(
                    owner,
                    command.session_id,
                    message=command.message,
                    page_context=command.page_context,
                    attachments=command.attachments,
                    erp=erp,
                )
            case "subscribe":
                return await self.subscribe(owner, command.session_id)
            case "interrupt" | "cancel" | "resume" | "confirm":
                async with self._lock:
                    user_id, row = await self._get_session(owner, command.session_id)
                    clients = {"erpnext": erp}
                    match command.action:
                        case "interrupt":
                            return await self._runtime.interrupt(
                                user_id, row.id, clients
                            )
                        case "cancel":
                            return await self._runtime.cancel(user_id, row.id, clients)
                        case "resume":
                            return await self._runtime.resume(user_id, row.id, clients)
                        case "confirm":
                            if command.confirmation is None:
                                raise AgentError("缺少工具确认事件。")
                            return await self._runtime.confirm(
                                user_id, row.id, command.confirmation, clients
                            )

    async def _get_session(
        self, owner: tuple[str, str], session_id: UUID | str | None
    ) -> tuple[str, SessionRecord]:
        if session_id is None:
            raise AgentError("缺少会话 ID。")
        user_id = self._user_id(owner)
        return user_id, await self._runtime.get(user_id, str(session_id))

    async def list(self, owner: tuple[str, str]) -> dict[str, list[SessionListItem]]:
        user_id = self._user_id(owner)
        return await self._runtime.list(user_id)

    async def create(self, owner: tuple[str, str]) -> dict[str, str]:
        user_id = self._user_id(owner)
        return {"id": (await self._runtime.create(user_id)).id}

    async def messages(
        self, owner: tuple[str, str], session_id: UUID | str | None
    ) -> SessionSnapshot:
        user_id, row = await self._get_session(owner, session_id)
        return await self._runtime.messages(user_id, row.id)

    async def rename(
        self, owner: tuple[str, str], session_id: UUID | str | None, title: str
    ) -> dict[str, bool]:
        user_id, row = await self._get_session(owner, session_id)
        return await self._runtime.rename(user_id, row.id, title)

    async def delete(
        self, owner: tuple[str, str], session_id: UUID | str | None, erp: ERPNext
    ) -> dict[str, bool]:
        async with self._lock:
            user_id, row = await self._get_session(owner, session_id)
            return await self._runtime.delete(user_id, row.id, {"erpnext": erp})

    async def upload(
        self,
        owner: tuple[str, str],
        session_id: UUID | str | None,
        upload: AttachmentUpload | None,
    ) -> AttachmentInfo:
        """验证会话归属，将上传文件保存到该会话的持久化工作空间。"""
        async with self._lock:
            user_id, row = await self._get_session(owner, session_id)
            if upload is None:
                raise AgentError("缺少上传文件。")
            return await self._attachments.save(user_id, row, upload)

    async def send(
        self,
        owner: tuple[str, str],
        session_id: UUID | str | None,
        *,
        message: str,
        page_context: PageContext | None,
        attachments: list[str],
        erp: ERPNext,
    ) -> ChatTriggerResponse:
        """按角色的图片输入设置组装消息，绑定 ERPNext 客户端并启动回复。"""
        async with self._lock:
            user_id, row = await self._get_session(owner, session_id)
            if not message.strip():
                raise AgentError("消息不能为空。")
            _, definition = self._catalog.definition(user_id, row.agent_id)
            files = await self._attachments.load(user_id, row, attachments)
            settings = self._catalog.definitions.models[definition.model]
            incoming = user_message(
                message.strip(),
                page_context,
                files,
                image_inputs=settings.image_inputs,
            )
            return await self._runtime.send(user_id, row.id, incoming, {"erpnext": erp})

    async def subscribe(
        self, owner: tuple[str, str], session_id: UUID | str | None
    ) -> StreamingResponse:
        user_id, row = await self._get_session(owner, session_id)
        return await self._runtime.subscribe(user_id, row.id)
