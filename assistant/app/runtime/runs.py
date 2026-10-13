"""会话运行控制；中断、取消和续跑遵循 AgentScope 原生语义。"""

import asyncio
from typing import Any

from agentscope.app._manager import ChatRunRegistry
from agentscope.app._router._schema import ChatTriggerResponse
from agentscope.app._service import ChatService, SessionService, SessionStatus
from agentscope.app.storage import SessionRecord, StorageBase
from agentscope.event import UserConfirmResultEvent
from agentscope.message import Msg, ToolCallState
from agentscope.types import ReplyFinishedReason
from starlette.datastructures import State

from .context import ContextRegistry
from .errors import RunError
from .teams import TeamService

type RunInput = Msg | UserConfirmResultEvent | None


class RunController:
    """在 Runtime 入口锁内绑定业务客户端，通过框架控制当前会话。"""

    def __init__(self, runtime: State, teams: TeamService) -> None:
        """绑定框架服务、业务上下文和权限请求的团队归属查询。"""
        self._storage: StorageBase = runtime.storage
        self._contexts: ContextRegistry = runtime.contexts
        self._teams = teams
        self._chat_service: ChatService = runtime.chat_service
        self._session_service: SessionService = runtime.session_service
        self._run_registry: ChatRunRegistry = runtime.chat_run_registry

    def _spawn(
        self, user_id: str, row: SessionRecord, incoming: RunInput
    ) -> asyncio.Task[None]:
        """通过框架任务注册器启动执行，防止会话重复运行。"""
        coroutine = self._chat_service.run(user_id, row.id, row.agent_id, incoming)
        try:
            return self._run_registry.spawn(coroutine, session_id=row.id)
        except RuntimeError:
            coroutine.close()
            raise RunError("当前会话正在执行。", "conflict") from None

    async def require_idle(self, user_id: str, row: SessionRecord) -> None:
        """检查当前回复是否正在运行，成员和后台工具不阻止新一轮输入。"""
        status = await self._session_service.get_session_status(
            user_id, row.agent_id, row.id
        )
        task = self._run_registry.get(row.id)
        if status == SessionStatus.RUNNING or (task is not None and not task.done()):
            raise RunError("当前会话正在执行。", "conflict")

    async def start(
        self,
        user_id: str,
        root: SessionRecord,
        incoming: Msg,
        clients: dict[str, Any],
    ) -> ChatTriggerResponse:
        """启动新的用户输入，保留同一会话已经保存的对话上下文。"""
        root = await self._contexts.root(user_id, root.id)
        await self.require_idle(user_id, root)
        status = await self._session_service.get_session_status(
            user_id, root.agent_id, root.id
        )
        if status in {
            SessionStatus.AWAITING_PERMISSION,
            SessionStatus.AWAITING_EXTERNAL_RESULT,
        }:
            raise RunError("请先处理当前会话的工具请求，或中断当前回复。", "conflict")
        self._contexts.bind(user_id, root.id, clients)
        self._spawn(user_id, root, incoming)
        return ChatTriggerResponse(session_id=root.id)

    async def resumable(self, user_id: str, root: SessionRecord) -> bool:
        """依据原生上下文、会话状态和回复结束原因判断是否展示续跑入口。"""
        root = await self._contexts.root(user_id, root.id)
        if not root.state.context:
            return False
        status = await self._session_service.get_session_status(
            user_id, root.agent_id, root.id
        )
        task = self._run_registry.get(root.id)
        if status != SessionStatus.IDLE or (task is not None and not task.done()):
            return False
        history, _ = await self._storage.list_messages(user_id, root.id, limit=1)
        return bool(
            history
            and history[-1].finished_reason
            in {
                ReplyFinishedReason.INTERRUPTED,
                ReplyFinishedReason.ERROR,
                ReplyFinishedReason.EXCEED_MAX_ITERS,
            }
        )

    async def resume(
        self, user_id: str, root: SessionRecord, clients: dict[str, Any]
    ) -> ChatTriggerResponse:
        """通过原生空输入继续当前会话，成员 Agent 由框架独立调度。"""
        root = await self._contexts.root(user_id, root.id)
        await self.require_idle(user_id, root)
        if not await self.resumable(user_id, root):
            raise RunError("当前会话没有可恢复的上下文。", "conflict")
        self._contexts.bind(user_id, root.id, clients)
        self._spawn(user_id, root, None)
        return ChatTriggerResponse(session_id=root.id)

    async def interrupt(
        self, user_id: str, row: SessionRecord, clients: dict[str, Any]
    ) -> dict[str, bool]:
        """提交原生中断请求，保留后台工具，由框架异步收尾当前回复。"""
        self._contexts.bind(user_id, row.id, clients)
        await self._chat_service.interrupt(user_id, row.id, row.agent_id)
        return {"ok": True}

    async def cancel(
        self, user_id: str, row: SessionRecord, clients: dict[str, Any]
    ) -> dict[str, bool]:
        """取消当前会话的回复及后台工具，保留上下文和团队。"""
        self._contexts.bind(user_id, row.id, clients)
        if not await self._session_service.cancel_session_run(row.id):
            raise RunError("会话停止超时，请重新读取状态后重试。", "unavailable")
        return {"ok": True}

    async def confirm(
        self,
        user_id: str,
        root: SessionRecord,
        event: UserConfirmResultEvent,
        clients: dict[str, Any],
    ) -> ChatTriggerResponse:
        """校验原生确认事件的归属，使用持久化调用信息恢复对应 Agent 会话。"""
        root = await self._contexts.root(user_id, root.id)
        for row in await self._teams.scope(user_id, root):
            if row.state.reply_id != event.reply_id:
                continue
            status = await self._session_service.get_session_status(
                user_id, row.agent_id, row.id
            )
            task = self._run_registry.get(row.id)
            if status != SessionStatus.AWAITING_PERMISSION or (
                task is not None and not task.done()
            ):
                raise RunError("工具确认已失效或正在处理。", "conflict")
            agent = await self._storage.get_agent(user_id, row.agent_id)
            if agent is None:
                break
            calls = {
                call.id: call
                for call in row.state.get_awaiting_tool_calls(agent.data.name)
                if call.state == ToolCallState.ASKING
            }
            ids = [result.tool_call.id for result in event.confirm_results]
            if not ids or len(ids) != len(set(ids)) or set(ids) - calls.keys():
                raise RunError("工具确认已失效或不属于当前任务。", "conflict")
            event = event.model_copy(
                update={
                    "confirm_results": [
                        result.model_copy(
                            update={"tool_call": calls[result.tool_call.id]}
                        )
                        for result in event.confirm_results
                    ]
                }
            )
            self._contexts.bind(user_id, root.id, clients)
            self._spawn(user_id, row, event)
            return ChatTriggerResponse(session_id=root.id)
        raise RunError("工具确认已失效或不属于当前任务。", "conflict")
