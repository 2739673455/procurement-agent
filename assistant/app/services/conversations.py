"""会话归属、目录操作和任务生命周期的业务协调。"""

import asyncio

from app.clients.erpnext.client import ERPNext
from app.contracts.conversations import Command
from app.errors.agent import AgentError
from app.repositories.conversations import ConversationRepository
from app.services.messages import public_messages
from app.services.runs import AgentRunService


class ConversationService:
    def __init__(self, repository: ConversationRepository, runs: AgentRunService):
        self.repository = repository
        self.runs = runs
        # 在同一临界区验证归属并启动或删除，避免删除后的任务重新创建状态。
        self._lock = asyncio.Lock()

    async def execute(self, command: Command, owner: tuple[str, str], erp: ERPNext):
        async with self._lock:
            if command.action == "list":
                return {"conversations": await self.repository.list(owner)}
            if command.action == "create":
                return {"id": await self.repository.create(owner)}
            if command.conversation_id is None:
                raise AgentError("缺少会话 ID。")
            identifier = str(command.conversation_id)
            await self.repository.require(owner, identifier)
            match command.action:
                case "messages":
                    return {
                        "messages": public_messages(
                            await self.runs.messages(identifier)
                        ),
                        "running": identifier in self.runs.runs,
                    }
                case "rename":
                    if not command.title.strip():
                        raise AgentError("标题不能为空。")
                    await self.repository.rename(identifier, command.title.strip())
                case "delete":
                    await self.runs.stop(identifier)
                    await self.runs.checkpointer.adelete_thread(identifier)
                    await self.repository.delete(identifier)
                case "stop":
                    await self.runs.stop(identifier)
                case "send":
                    if not command.message.strip():
                        raise AgentError("消息不能为空。")
                    return self.runs.start(
                        identifier,
                        erp,
                        command.message.strip(),
                        command.page_context,
                        command.attachments,
                    )
                case "subscribe":
                    return self.runs.subscribe(identifier)
            return {"ok": True}
