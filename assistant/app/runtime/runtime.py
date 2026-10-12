"""类型化运行入口；宿主提供身份与原生消息，框架管理执行和持久化。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import TracebackType
from typing import Any, Self, TypedDict
from uuid import uuid4

from agentscope.app._manager import ChatRunRegistry
from agentscope.app._router._schema import ChatTriggerResponse
from agentscope.app._router._session import stream_session_events
from agentscope.app._service import SessionService as NativeSessionService
from agentscope.app._service import SessionStatus
from agentscope.app.message_bus import MessageBus
from agentscope.app.storage import (
    SessionConfig,
    SessionNaming,
    SessionRecord,
    StorageBase,
)
from agentscope.app.workspace_manager import WorkspaceManagerBase
from agentscope.event import UserConfirmResultEvent
from agentscope.message import Msg
from fastapi import FastAPI
from starlette.responses import StreamingResponse

from .agents import AgentRegistry
from .catalog import AgentCatalog
from .context import CONTROL_KEY, ContextRegistry
from .errors import RunError
from .runs import RunController
from .teams import TeamService, TeamSummary


class SessionListItem(TypedDict):
    """会话列表项。"""

    id: str
    title: str
    updated_at: datetime


class SessionSnapshot(TeamSummary):
    """会话历史与团队运行状态的类型化快照。"""

    messages: list[Msg]
    intent: str
    status: SessionStatus | None
    resumable: bool


class Runtime:
    """单次 async with 生命周期内使用的通用会话与运行 API。

    user_id 由宿主认证后提供；clients 由宿主管理生命周期，仅保存在内存中。
    返回框架原生消息/事件，未公开原生 HTTP 路由或 app.state。
    """

    def __init__(
        self,
        app: FastAPI,
        catalog: AgentCatalog,
        workspace_manager: WorkspaceManagerBase,
    ) -> None:
        self._app = app
        self.catalog = catalog
        self.workspace_manager = workspace_manager
        self._lifespan = app.router.lifespan_context(app)
        self._entered = False
        self._used = False
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        async with self._lock:
            if self._used:
                raise RuntimeError("Runtime 生命周期只能进入一次。")
            self._used = True
            await self._lifespan.__aenter__()
            state = self._app.state
            self._storage: StorageBase = state.storage
            self._agents: AgentRegistry = state.agents
            self._contexts: ContextRegistry = state.contexts
            self._session_service: NativeSessionService = state.session_service
            self._teams: TeamService = state.teams
            self._runs: RunController = state.runs
            self._message_bus: MessageBus = state.message_bus
            self._run_registry: ChatRunRegistry = state.chat_run_registry
            self._entered = True
            return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        async with self._lock:
            self._entered = False
            await self._lifespan.__aexit__(exc_type, exc, tb)

    def _require_open(self) -> None:
        if not self._entered:
            raise RuntimeError("请在 async with Runtime 生命周期内调用。")

    async def get(self, user_id: str, session_id: str) -> SessionRecord:
        """读取所属用户会话，成员 Agent 会话通过 Agent 团队范围操作。"""
        async with self._lock:
            return await self._get_session(user_id, session_id)

    async def _get_session(self, user_id: str, session_id: str) -> SessionRecord:
        """在入口操作锁内校验生命周期、角色及用户归属。"""
        self._require_open()
        row = await self._storage.get_session(user_id, "", str(session_id))
        references = {
            self._agents.reference_session_id(user_id, key)
            for key in self.catalog.definitions.agents
        }
        if row is None or row.origin.type != "user" or row.id in references:
            raise RunError("会话不存在或无权访问。", "not_found")
        self.catalog.definition(user_id, row.agent_id)
        return row

    async def list(self, user_id: str) -> dict[str, list[SessionListItem]]:
        """列出用户会话，排除框架 Agent 团队会话及角色参考会话。"""
        async with self._lock:
            self._require_open()
            sessions: list[SessionListItem] = []
            for key in self.catalog.definitions.agents:
                rows = await self._storage.list_sessions(
                    user_id, self.catalog.agent_id(user_id, key)
                )
                for row in rows:
                    if (
                        row.origin.type == "user"
                        and row.id != self._agents.reference_session_id(user_id, key)
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

    async def create(
        self, user_id: str, *, agent: str | None = None, title: str = "新对话"
    ) -> SessionRecord:
        """创建默认入口 Agent 的会话，并为原生 AgentInvite 工具登记成员 Agent 模型配置。"""
        async with self._lock:
            self._require_open()
            key = agent if agent is not None else self.catalog.definitions.default
            if key not in self.catalog.definitions.agents:
                raise RunError("当前角色未配置。", "not_found")
            agent_id = self.catalog.agent_id(user_id, key)
            models = await self._agents.configure_user(user_id)
            identifier = str(uuid4())
            workspace_id = await self.workspace_manager.assign_workspace_id(
                user_id=user_id, agent_id=agent_id, session_id=identifier
            )
            row = await self._storage.upsert_session(
                user_id,
                agent_id,
                SessionConfig(
                    workspace_id=workspace_id,
                    name=title,
                    naming=SessionNaming(auto=False),
                    chat_model_config=models[key],
                ),
                session_id=identifier,
            )
            return row

    async def messages(self, user_id: str, session_id: str) -> SessionSnapshot:
        """读取完整原生消息、Agent 团队状态、待确认调用以及恢复条件。"""
        async with self._lock:
            row = await self._get_session(user_id, session_id)
            history: list[Msg] = []
            before: str | None = None
            while True:
                page, more = await self._storage.list_messages(
                    user_id, row.id, limit=100, before=before
                )
                history = page + history
                if not more:
                    break
                before = page[0].id
            summary = await self._teams.summary(user_id, row)
            return {
                "messages": history,
                "intent": row.state.middle_context.get(CONTROL_KEY) or "active",
                **summary,
                "status": await self._session_service.get_session_status(
                    user_id, row.agent_id, row.id
                ),
                "resumable": not summary["running"]
                and await self._runs.resumable(user_id, row),
            }

    async def rename(
        self, user_id: str, session_id: str, title: str
    ) -> dict[str, bool]:
        """修改整个任务范围空闲的会话标题。"""
        async with self._lock:
            row = await self._get_session(user_id, session_id)
            if not title.strip():
                raise RunError("标题不能为空。", "invalid_input")
            await self._runs.require_idle(user_id, row)
            row.config.name = title.strip()
            row.config.naming = SessionNaming(auto=False)
            await self._storage.upsert_session(
                user_id, row.agent_id, row.config, session_id=row.id
            )
            return {"ok": True}

    async def delete(
        self, user_id: str, session_id: str, clients: dict[str, Any] | None = None
    ) -> dict[str, bool]:
        """停止整个任务后由框架删除会话、Agent 团队、消息及工作空间会话资源。"""
        async with self._lock:
            row = await self._get_session(user_id, session_id)
            await self._runs.stop(user_id, row, clients or {}, intent="cancelled")
            await self._session_service.delete_session(user_id, row.agent_id, row.id)
            self._contexts.forget(row.id)
            return {"ok": True}

    async def send(
        self,
        user_id: str,
        session_id: str,
        incoming: Msg,
        clients: dict[str, Any] | None = None,
    ) -> ChatTriggerResponse:
        """刷新角色配置并提交原生消息；与删除、改名及运行控制串行。"""
        async with self._lock:
            row = await self._get_session(user_id, session_id)
            await self._runs.require_idle(user_id, row)
            key, _ = self.catalog.definition(user_id, row.agent_id)
            models = await self._agents.configure_user(user_id)
            row.config.chat_model_config = models[key]
            await self._storage.upsert_session(
                user_id, row.agent_id, row.config, session_id=row.id
            )
            return await self._runs.start(user_id, row, incoming, clients or {})

    async def interrupt(
        self, user_id: str, session_id: str, clients: dict[str, Any] | None = None
    ) -> dict[str, bool]:
        """停止整个团队，保留可恢复上下文。"""
        async with self._lock:
            row = await self._get_session(user_id, session_id)
            return await self._runs.stop(
                user_id, row, clients or {}, intent="interrupted"
            )

    async def cancel(
        self, user_id: str, session_id: str, clients: dict[str, Any] | None = None
    ) -> dict[str, bool]:
        """取消任务并解散团队，保留主会话历史。"""
        async with self._lock:
            row = await self._get_session(user_id, session_id)
            return await self._runs.stop(
                user_id, row, clients or {}, intent="cancelled"
            )

    async def resume(
        self, user_id: str, session_id: str, clients: dict[str, Any] | None = None
    ) -> ChatTriggerResponse:
        """使用保存的上下文恢复整个任务。"""
        async with self._lock:
            row = await self._get_session(user_id, session_id)
            return await self._runs.resume(user_id, row, clients or {})

    async def confirm(
        self,
        user_id: str,
        session_id: str,
        event: UserConfirmResultEvent,
        clients: dict[str, Any] | None = None,
    ) -> ChatTriggerResponse:
        """校验并提交框架原生权限确认。"""
        async with self._lock:
            row = await self._get_session(user_id, session_id)
            return await self._runs.confirm(user_id, row, event, clients or {})

    async def wait(self, user_id: str, session_id: str) -> None:
        """等待当前主会话执行结束；不代表团队或后台工具全部结束。"""
        async with self._lock:
            row = await self._get_session(user_id, session_id)
            task = self._run_registry.get(row.id)
        if task is not None:
            await asyncio.shield(task)

    async def subscribe(self, user_id: str, session_id: str) -> StreamingResponse:
        """校验归属并返回原生 SSE 响应，交由宿主 HTTP 服务消费。"""
        async with self._lock:
            row = await self._get_session(user_id, session_id)
            return await stream_session_events(
                session_id=row.id,
                agent_id=row.agent_id,
                user_id=user_id,
                storage=self._storage,
                message_bus=self._message_bus,
            )
