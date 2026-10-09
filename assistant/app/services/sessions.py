"""认证用户的会话入口；角色、运行与团队控制由各自服务组织。"""

import asyncio
from uuid import uuid4

from agentscope.app._router._session import stream_session_events
from agentscope.app.storage import SessionConfig, SessionNaming

from app.contracts.sessions import Command
from app.errors.agent import AgentError
from app.runtime.context import CONTROL_KEY
from app.services.agents import AgentService
from app.services.inputs import user_message
from app.services.runs import RunService
from app.services.teams import TeamService


class SessionService:
    """协调会话请求和并发操作，消息与执行状态使用框架原生存储。"""

    def __init__(self, runtime):
        """装配角色、团队及运行服务，并创建入口操作锁。"""
        self.runtime = runtime
        self.storage = runtime.storage
        self.agents = AgentService(runtime)
        self.teams = TeamService(runtime)
        self.runs = RunService(runtime, self.teams)
        self._lock = asyncio.Lock()

    async def execute(self, command: Command, owner, erp):
        """分发已认证请求，各操作分别检查会话归属和运行条件。"""
        match command.action:
            case "agents":
                return self.agents.list()
            case "list":
                return await self.list(owner)
            case "create":
                return await self.create(owner, command.agent_key)
            case "messages":
                return await self.messages(owner, command.session_id)
            case "rename":
                return await self.rename(owner, command.session_id, command.title)
            case "delete":
                return await self.delete(owner, command.session_id, erp)
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

    async def _get_session(self, owner, session_id):
        """核实用户会话归属，团队内部会话通过团队范围操作。"""
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

    async def list(self, owner):
        """列出全部入口角色的用户会话，排除框架团队及角色参考会话。"""
        async with self._lock:
            user_id = self.agents.catalog.user_id(owner)
            sessions = []
            for key, definition in self.agents.catalog.definitions.agents.items():
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
                                "agent_key": key,
                                "agent_name": definition.name,
                            }
                        )
            return {
                "sessions": sorted(
                    sessions, key=lambda row: row["updated_at"], reverse=True
                )
            }

    async def create(self, owner, agent_key=None):
        """创建选定角色的会话，并将成员模型配置登记到原生邀请机制。"""
        async with self._lock:
            key = agent_key or self.agents.catalog.definitions.default
            if key not in self.agents.catalog.definitions.agents:
                raise AgentError("角色不存在。", 404)
            user_id, agent_id = self.agents.identity(owner, key)
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

    async def messages(self, owner, session_id):
        """读取完整原生消息、团队状态、待确认调用以及恢复条件。"""
        async with self._lock:
            user_id, row = await self._get_session(owner, session_id)
            history = []
            before = None
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

    async def rename(self, owner, session_id, title):
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

    async def delete(self, owner, session_id, erp):
        """停止整个任务后由框架删除会话、团队、消息及工作空间会话资源。"""
        async with self._lock:
            user_id, row = await self._get_session(owner, session_id)
            await self.runs.stop(user_id, row, {"erpnext": erp}, intent="cancelled")
            await self.runtime.session_service.delete_session(
                user_id, row.agent_id, row.id
            )
            self.runtime.contexts.forget(row.id)
            return {"ok": True}

    async def send(self, owner, session_id, *, message, page_context, attachments, erp):
        """装配用户输入和角色配置，通过运行服务启动框架任务。"""
        async with self._lock:
            user_id, row = await self._get_session(owner, session_id)
            if not message.strip():
                raise AgentError("消息不能为空。")
            await self.runs.require_idle(user_id, row)
            key, _ = self.agents.catalog.definition(user_id, row.agent_id)
            models = await self.agents.configure_user(user_id)
            row.config.chat_model_config = models[key]
            await self.storage.upsert_session(
                user_id, row.agent_id, row.config, session_id=row.id
            )
            incoming = await asyncio.to_thread(
                user_message, message.strip(), page_context, attachments
            )
            return await self.runs.start(user_id, row, incoming, {"erpnext": erp})

    async def subscribe(self, owner, session_id):
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
