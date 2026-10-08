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
from app.errors.agent import AgentError
from app.services.events import session_events
from app.services.inputs import user_message
from app.services.messages import public_messages


class ConversationService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.storage = runtime.storage
        self._lock = asyncio.Lock()

    @staticmethod
    def identity(owner):
        user_id = json.dumps(owner, ensure_ascii=False, separators=(",", ":"))
        return user_id, str(uuid5(NAMESPACE_URL, "procurement:" + user_id))

    async def configure(self, user_id, agent_id):
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

    async def execute(self, command, owner, erp):
        user_id, agent_id = self.identity(owner)
        async with self._lock:
            if command.action == "list":
                rows = await self.storage.list_sessions(user_id, agent_id)
                return {
                    "conversations": [
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
            if command.action == "create":
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
            if command.conversation_id is None:
                raise AgentError("缺少会话 ID。")
            identifier = str(command.conversation_id)
            row = await self.storage.get_session(user_id, agent_id, identifier)
            if row is None:
                raise AgentError("会话不存在或无权访问。", 404)
            registry = self.runtime.chat_run_registry
            match command.action:
                case "messages":
                    history = []
                    before = None
                    while True:
                        page, more = await self.storage.list_messages(
                            user_id, identifier, limit=100, before=before
                        )
                        history = page + history
                        if not more:
                            break
                        before = page[0].id
                    status = await self.runtime.session_service.get_session_status(
                        user_id, agent_id, identifier
                    )
                    return {
                        "messages": public_messages(history),
                        "running": registry.get(identifier) is not None
                        or status == SessionStatus.RUNNING,
                    }
                case "rename":
                    if not command.title.strip():
                        raise AgentError("标题不能为空。")
                    task = registry.get(identifier)
                    if task is not None and not task.done():
                        raise AgentError("请等待本轮执行结束后再修改标题。", 409)
                    row.config.name = command.title.strip()
                    row.config.naming = SessionNaming(auto=False)
                    await self.storage.upsert_session(
                        user_id,
                        agent_id,
                        row.config,
                        session_id=identifier,
                    )
                case "delete":
                    await self.runtime.session_service.delete_session(
                        user_id, agent_id, identifier
                    )
                case "stop":
                    task = registry.get(identifier)
                    if task is not None:
                        await self.runtime.session_service.cancel_session_run(
                            identifier
                        )
                        await asyncio.gather(task, return_exceptions=True)
                case "send":
                    if not command.message.strip():
                        raise AgentError("消息不能为空。")
                    task = registry.get(identifier)
                    if task is not None and not task.done():
                        raise AgentError("当前对话正在执行。", 409)
                    row.config.chat_model_config = await self.configure(
                        user_id, agent_id
                    )
                    await self.storage.upsert_session(
                        user_id,
                        agent_id,
                        row.config,
                        session_id=identifier,
                    )
                    incoming = await asyncio.to_thread(
                        user_message,
                        command.message.strip(),
                        command.page_context,
                        command.attachments,
                    )
                    token = erp_context.set(erp)
                    try:
                        task = registry.spawn(
                            self.runtime.chat_service.run(
                                user_id, identifier, agent_id, incoming
                            ),
                            session_id=identifier,
                        )
                    finally:
                        erp_context.reset(token)
                    return session_events(self.runtime, user_id, identifier, task)
                case "subscribe":
                    return session_events(
                        self.runtime, user_id, identifier, registry.get(identifier)
                    )
            return {"ok": True}
