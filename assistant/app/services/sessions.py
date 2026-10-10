"""认证用户的会话入口；Agent 角色、运行与 Agent 团队控制由各自服务组织。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import TypedDict
from uuid import UUID, uuid4

from agentscope.app._router._schema import ChatTriggerResponse
from agentscope.app._router._session import stream_session_events
from agentscope.app.storage import (
    SessionConfig,
    SessionNaming,
    SessionRecord,
    StorageBase,
)
from agentscope.message import Msg
from starlette.datastructures import State
from starlette.responses import StreamingResponse

from app.clients.erpnext.client import ERPNext
from app.config.agents import cfg
from app.contracts.sessions import (
    AttachmentInfo,
    AttachmentUpload,
    Command,
    PageContext,
)
from app.errors.agent import AgentError
from app.runtime.context import CONTROL_KEY
from app.services.agents import AgentService
from app.services.attachments import AttachmentService
from app.services.inputs import user_message
from app.services.runs import RunService
from app.services.teams import TeamService


class SessionListItem(TypedDict):
    """会话列表项。"""

    id: str
    title: str
    updated_at: datetime


class SessionService:
    """协调会话请求和并发操作，消息与执行状态使用框架原生存储。"""

    def __init__(self, runtime: State) -> None:
        """装配角色、Agent 团队及运行服务，并创建入口操作锁。"""
        self.runtime = runtime
        self.storage: StorageBase = runtime.storage
        self.agents = AgentService(runtime)
        self.attachments = AttachmentService(runtime.workspace_manager)
        self.teams = TeamService(runtime)
        self.runs = RunService(runtime, self.teams)
        self._lock = asyncio.Lock()

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
                        case "interrupt" | "cancel":
                            return await self.runs.stop(
                                user_id,
                                row,
                                clients,
                                intent="interrupted"
                                if command.action == "interrupt"
                                else "cancelled",
                            )
                        case "resume":
                            return await self.runs.resume(user_id, row, clients)
                        case "confirm":
                            if command.confirmation is None:
                                raise AgentError("缺少工具确认事件。")
                            return await self.runs.confirm(
                                user_id, row, command.confirmation, clients
                            )

    async def _get_session(
        self, owner: tuple[str, str], session_id: UUID | str | None
    ) -> tuple[str, SessionRecord]:
        """核实用户会话归属，成员 Agent 会话通过 Agent 团队范围操作。"""
        if session_id is None:
            raise AgentError("缺少会话 ID。")
        user_id = self.agents.catalog.user_id(owner)
        row = await self.storage.get_session(user_id, "", str(session_id))
        references = {
            self.agents.reference_session_id(user_id, key)
            for key in self.agents.catalog.definitions.agents
        }
        if row is None or row.origin.type != "user" or row.id in references:
            raise AgentError("会话不存在或无权访问。", 404)
        self.agents.catalog.definition(user_id, row.agent_id)
        return user_id, row

    async def list(self, owner: tuple[str, str]) -> dict[str, list[SessionListItem]]:
        """列出用户会话，排除框架 Agent 团队会话及角色参考会话。"""
        async with self._lock:
            user_id = self.agents.catalog.user_id(owner)
            sessions: list[SessionListItem] = []
            for key in self.agents.catalog.definitions.agents:
                rows = await self.storage.list_sessions(
                    user_id, self.agents.catalog.agent_id(user_id, key)
                )
                for row in rows:
                    if (
                        row.origin.type == "user"
                        and row.id != self.agents.reference_session_id(user_id, key)
                    ):
                        sessions.append(
                            {
                                "id": row.id,
                                "title": row.config.name,
                                "updated_at": row.updated_at,
                            }
                        )
            return {
                "sessions": sorted(
                    sessions, key=lambda row: row["updated_at"], reverse=True
                )
            }

    async def create(self, owner: tuple[str, str]) -> dict[str, str]:
        """创建默认入口 Agent 的会话，并为原生 AgentInvite 工具登记成员 Agent 模型配置。"""
        async with self._lock:
            key = self.agents.catalog.definitions.default
            user_id, agent_id = self.agents.identity(owner)
            models = await self.agents.configure_user(user_id)
            identifier = str(uuid4())
            workspace_id = await self.runtime.workspace_manager.assign_workspace_id(
                user_id=user_id, agent_id=agent_id, session_id=identifier
            )
            row = await self.storage.upsert_session(
                user_id,
                agent_id,
                SessionConfig(
                    workspace_id=workspace_id,
                    name="新对话",
                    naming=SessionNaming(auto=False),
                    chat_model_config=models[key],
                ),
                session_id=identifier,
            )
            return {"id": row.id}

    async def messages(
        self, owner: tuple[str, str], session_id: UUID | str | None
    ) -> dict[str, object]:
        """读取完整原生消息、Agent 团队状态、待确认调用以及恢复条件。"""
        async with self._lock:
            user_id, row = await self._get_session(owner, session_id)
            history: list[Msg] = []
            before: str | None = None
            while True:
                page, more = await self.storage.list_messages(
                    user_id, row.id, limit=100, before=before
                )
                history = page + history
                if not more:
                    break
                before = page[0].id
            summary = await self.teams.summary(user_id, row)
            return {
                "messages": history,
                "intent": row.state.middle_context.get(CONTROL_KEY) or "active",
                **summary,
                "status": await self.runtime.session_service.get_session_status(
                    user_id, row.agent_id, row.id
                ),
                "resumable": not summary["running"]
                and await self.runs.resumable(user_id, row),
            }

    async def rename(
        self, owner: tuple[str, str], session_id: UUID | str | None, title: str
    ) -> dict[str, bool]:
        """修改整个任务范围空闲的会话标题。"""
        async with self._lock:
            user_id, row = await self._get_session(owner, session_id)
            if not title.strip():
                raise AgentError("标题不能为空。")
            await self.runs.require_idle(user_id, row)
            row.config.name = title.strip()
            row.config.naming = SessionNaming(auto=False)
            await self.storage.upsert_session(
                user_id, row.agent_id, row.config, session_id=row.id
            )
            return {"ok": True}

    async def delete(
        self, owner: tuple[str, str], session_id: UUID | str | None, erp: ERPNext
    ) -> dict[str, bool]:
        """停止整个任务后由框架删除会话、Agent 团队、消息及工作空间会话资源。"""
        async with self._lock:
            user_id, row = await self._get_session(owner, session_id)
            await self.runs.stop(user_id, row, {"erpnext": erp}, intent="cancelled")
            await self.runtime.session_service.delete_session(
                user_id, row.agent_id, row.id
            )
            self.runtime.contexts.forget(row.id)
            return {"ok": True}

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
            return await self.attachments.save(user_id, row, upload)

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
        """装配用户输入和角色配置，通过运行服务启动框架任务。"""
        async with self._lock:
            user_id, row = await self._get_session(owner, session_id)
            if not message.strip():
                raise AgentError("消息不能为空。")
            await self.runs.require_idle(user_id, row)
            key, definition = self.agents.catalog.definition(user_id, row.agent_id)
            models = await self.agents.configure_user(user_id)
            row.config.chat_model_config = models[key]
            await self.storage.upsert_session(
                user_id, row.agent_id, row.config, session_id=row.id
            )
            files = await self.attachments.load(user_id, row, attachments)
            model = cfg.models[definition.model]
            incoming = user_message(
                message.strip(), page_context, files, image_inputs=model.image_inputs
            )
            return await self.runs.start(user_id, row, incoming, {"erpnext": erp})

    async def subscribe(
        self, owner: tuple[str, str], session_id: UUID | str | None
    ) -> StreamingResponse:
        """核实归属并返回框架原生事件长连接。"""
        async with self._lock:
            user_id, row = await self._get_session(owner, session_id)
            return await stream_session_events(
                session_id=row.id,
                agent_id=row.agent_id,
                user_id=user_id,
                storage=self.storage,
                message_bus=self.runtime.message_bus,
            )
