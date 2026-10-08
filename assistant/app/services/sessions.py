"""ERPNext 会话业务：身份映射与展示协议，执行和存储由 AgentScope 管理。"""

import asyncio
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app._service import SessionStatus
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    ChatModelConfig,
    SessionConfig,
    SessionNaming,
)

from app.agent.model import ChatCredential
from app.agent.prompts import SYSTEM
from app.agent.runtime import erp_context
from app.config import app_config
from app.contracts.sessions import Command
from app.errors.agent import AgentError
from app.services.events import session_events
from app.services.inputs import user_message
from app.services.messages import public_messages


class SessionService:
    """协调 ERPNext 用户会话操作，使用 AgentScope 执行任务和持久化数据。"""

    def __init__(self, runtime):
        """绑定框架运行服务，并创建会话操作锁以协调并发请求。"""
        self.runtime = runtime
        self.storage = runtime.storage
        self._lock = asyncio.Lock()

    @staticmethod
    def identity(owner):
        """将站点和用户名映射为框架用户标识与采购助手的固定 Agent ID。"""
        user_id = json.dumps(owner, ensure_ascii=False, separators=(",", ":"))
        return user_id, str(uuid5(NAMESPACE_URL, "procurement:" + user_id))

    async def configure(self, user_id, agent_id):
        """保存服务端模型配置引用，并配置当前用户的采购 Agent。"""
        key = app_config.cfg.lm_config.active
        credential = ChatCredential(
            id=str(uuid5(NAMESPACE_URL, user_id + ":" + key)),
            name=key,
            config_key=key,
        )
        await self.storage.upsert_credential(user_id, credential)
        await self.storage.upsert_agent(
            user_id,
            AgentRecord(
                id=agent_id,
                user_id=user_id,
                data=AgentData(
                    id=agent_id,
                    name="procurement_assistant",
                    system_prompt=SYSTEM,
                    context_config=ContextConfig(),
                    react_config=ReActConfig(interruption_message="已停止生成。"),
                ),
            ),
        )
        return ChatModelConfig(
            type=credential.type,
            credential_id=credential.id,
            model=credential.settings.model,
            parameters={},
        )

    async def execute(self, command: Command, owner, erp):
        """按入口协议分发请求，每项操作自行校验归属并控制并发。"""
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
                return await self.delete(owner, command.session_id)
            case "stop":
                return await self.stop(owner, command.session_id)
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

    async def _get_session(self, owner, session_id):
        """按站点和用户查找会话，供持有操作锁的方法调用。"""
        if session_id is None:
            raise AgentError("缺少会话 ID。")
        user_id, agent_id = self.identity(owner)
        row = await self.storage.get_session(user_id, agent_id, str(session_id))
        if row is None:
            raise AgentError("会话不存在或无权访问。", 404)
        return user_id, agent_id, row

    async def list(self, owner):
        """列出用户所属的会话，按更新时间降序排列。"""
        async with self._lock:
            user_id, agent_id = self.identity(owner)
            rows = await self.storage.list_sessions(user_id, agent_id)
            return {
                "sessions": [
                    {
                        "id": row.id,
                        "title": row.config.name,
                        "updated_at": row.updated_at,
                    }
                    for row in sorted(
                        rows, key=lambda row: row.updated_at, reverse=True
                    )
                ]
            }

    async def create(self, owner):
        """配置模型与 Agent，并创建用户所属的会话。"""
        async with self._lock:
            user_id, agent_id = self.identity(owner)
            model = await self.configure(user_id, agent_id)
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
                    chat_model_config=model,
                ),
                session_id=identifier,
            )
            return {"id": row.id}

    async def messages(self, owner, session_id):
        """读取持久化消息和运行状态。"""
        async with self._lock:
            user_id, agent_id, row = await self._get_session(owner, session_id)
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
            status = await self.runtime.session_service.get_session_status(
                user_id, agent_id, row.id
            )
            return {
                "messages": public_messages(history),
                "running": self.runtime.chat_run_registry.get(row.id) is not None
                or status == SessionStatus.RUNNING,
            }

    async def rename(self, owner, session_id, title):
        """修改空闲会话的标题。"""
        async with self._lock:
            user_id, agent_id, row = await self._get_session(owner, session_id)
            if not title.strip():
                raise AgentError("标题不能为空。")
            task = self.runtime.chat_run_registry.get(row.id)
            if task is not None and not task.done():
                raise AgentError("请等待本轮执行结束后再修改标题。", 409)
            row.config.name = title.strip()
            row.config.naming = SessionNaming(auto=False)
            await self.storage.upsert_session(
                user_id, agent_id, row.config, session_id=row.id
            )
            return {"ok": True}

    async def delete(self, owner, session_id):
        """通过框架取消运行并删除会话及其数据。"""
        async with self._lock:
            user_id, agent_id, row = await self._get_session(owner, session_id)
            await self.runtime.session_service.delete_session(user_id, agent_id, row.id)
            return {"ok": True}

    async def stop(self, owner, session_id):
        """取消当前运行，并等待框架完成状态保存。"""
        async with self._lock:
            _, _, row = await self._get_session(owner, session_id)
            task = self.runtime.chat_run_registry.get(row.id)
            if task is not None:
                await self.runtime.session_service.cancel_session_run(row.id)
                await asyncio.gather(task, return_exceptions=True)
            return {"ok": True}

    async def send(self, owner, session_id, *, message, page_context, attachments, erp):
        """整理输入并启动框架后台任务，返回本轮事件订阅。"""
        async with self._lock:
            user_id, agent_id, row = await self._get_session(owner, session_id)
            if not message.strip():
                raise AgentError("消息不能为空。")
            registry = self.runtime.chat_run_registry
            task = registry.get(row.id)
            if task is not None and not task.done():
                raise AgentError("当前对话正在执行。", 409)
            row.config.chat_model_config = await self.configure(user_id, agent_id)
            await self.storage.upsert_session(
                user_id, agent_id, row.config, session_id=row.id
            )
            incoming = await asyncio.to_thread(
                user_message, message.strip(), page_context, attachments
            )
            token = erp_context.set(erp)
            try:
                task = registry.spawn(
                    self.runtime.chat_service.run(user_id, row.id, agent_id, incoming),
                    session_id=row.id,
                )
            finally:
                erp_context.reset(token)
            return session_events(self.runtime, user_id, row.id, task)

    async def subscribe(self, owner, session_id):
        """订阅会话事件，不启动新的 Agent 运行。"""
        async with self._lock:
            user_id, _, row = await self._get_session(owner, session_id)
            return session_events(
                self.runtime,
                user_id,
                row.id,
                self.runtime.chat_run_registry.get(row.id),
            )
