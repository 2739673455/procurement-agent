"""通用任务控制；执行、工具收尾、状态保存与事件发布均复用 AgentScope。"""

import asyncio
from typing import Any

from agentscope.app._router._schema import ChatTriggerResponse
from agentscope.app._service import SessionStatus
from agentscope.app.message_bus import MessageBusKeys
from agentscope.app.storage import SessionRecord, StorageBase
from agentscope.event import UserConfirmResultEvent, UserInterruptEvent
from agentscope.message import Msg, ToolCallState
from agentscope.types import ReplyFinishedReason
from starlette.datastructures import State

from app.errors.agent import AgentError
from app.runtime.context import CONTROL_KEY, ContextRegistry, RunIntent
from app.runtime.models import model_client_scope
from app.services.teams import TeamService

type RunInput = Msg | UserConfirmResultEvent | UserInterruptEvent | None


class RunService:
    """协调会话的用户业务客户端、框架后台运行以及整个 Agent 团队的中断和取消。"""

    def __init__(self, runtime: State, teams: TeamService) -> None:
        """绑定框架服务、上下文提供器和 Agent 团队范围解析器。"""
        self.runtime = runtime
        self.storage: StorageBase = runtime.storage
        self.contexts: ContextRegistry = runtime.contexts
        self.teams = teams

    async def _invoke(
        self, user_id: str, row: SessionRecord, incoming: RunInput
    ) -> None:
        """保持完整运行的客户端作用域，框架负责推理与持久化。"""
        async with model_client_scope():
            await self.runtime.chat_service.run(user_id, row.id, row.agent_id, incoming)

    def _spawn(
        self, user_id: str, row: SessionRecord, incoming: RunInput
    ) -> asyncio.Task[None]:
        """通过框架任务注册器启动执行，防止会话重复运行。"""
        coroutine = self._invoke(user_id, row, incoming)
        try:
            return self.runtime.chat_run_registry.spawn(coroutine, session_id=row.id)
        except RuntimeError:
            coroutine.close()
            raise AgentError("当前会话正在执行。", 409) from None

    async def require_idle(self, user_id: str, row: SessionRecord) -> None:
        """使用框架状态及待启动任务检查整个任务范围是否空闲。"""
        if (await self.teams.summary(user_id, row))["running"]:
            raise AgentError("当前任务正在执行。", 409)

    async def _activate(
        self, user_id: str, root: SessionRecord, clients: dict[str, Any]
    ) -> None:
        """绑定本次请求的用户业务客户端，并持久清除 Agent 团队的中断意图。"""
        self.contexts.bind(user_id, root.id, clients)
        for row in await self.teams.scope(user_id, root):
            row.state.middle_context[CONTROL_KEY] = "active"
            await self.storage.update_session_state(
                user_id, row.agent_id, row.id, row.state
            )

    async def start(
        self,
        user_id: str,
        root: SessionRecord,
        incoming: Msg,
        clients: dict[str, Any],
    ) -> ChatTriggerResponse:
        """启动新的用户输入，保留同一会话已经保存的对话上下文。"""
        await self.require_idle(user_id, root)
        summary = await self.teams.summary(user_id, root)
        if summary["confirmations"]:
            raise AgentError("请先处理工具确认，或取消当前任务。", 409)
        await self._activate(user_id, root, clients)
        self._spawn(user_id, root, incoming)
        return ChatTriggerResponse(session_id=root.id)

    async def resumable(self, user_id: str, root: SessionRecord) -> bool:
        """依据框架上下文、回复终态和持久意图判断是否可以无新消息续跑。"""
        if (
            root.state.middle_context.get(CONTROL_KEY) == "cancelled"
            or not root.state.context
        ):
            return False
        status = await self.runtime.session_service.get_session_status(
            user_id, root.agent_id, root.id
        )
        if status != SessionStatus.IDLE:
            return False
        history, _ = await self.storage.list_messages(user_id, root.id, limit=1)
        return root.state.middle_context.get(CONTROL_KEY) == "interrupted" or bool(
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
        """加载已保存的上下文继续执行，并恢复中断的成员 Agent。"""
        await self.require_idle(user_id, root)
        if not await self.resumable(user_id, root):
            raise AgentError("当前任务没有可恢复的中断上下文。", 409)
        rows = await self.teams.scope(user_id, root)
        await self._activate(user_id, root, clients)
        for row in rows[1:]:
            history, _ = await self.storage.list_messages(user_id, row.id, limit=1)
            if history and history[-1].finished_reason in {
                ReplyFinishedReason.INTERRUPTED,
                ReplyFinishedReason.ERROR,
                ReplyFinishedReason.EXCEED_MAX_ITERS,
            }:
                self._spawn(user_id, row, None)
        self._spawn(user_id, root, None)
        return ChatTriggerResponse(session_id=root.id)

    async def stop(
        self,
        user_id: str,
        root: SessionRecord,
        clients: dict[str, Any],
        *,
        intent: RunIntent,
    ) -> dict[str, bool]:
        """停止负责人 Agent 及成员 Agent，等待框架保存状态；取消同时解散 Agent 团队。"""
        self.contexts.bind(user_id, root.id, clients)
        self.contexts.block(root.id, intent)
        rows = await self.teams.scope(user_id, root)
        pending = rows
        stopped: set[str] = set()
        while pending:
            session_ids = {row.id for row in pending}
            tasks = {
                task
                for row in pending
                if (task := self.runtime.chat_run_registry.get(row.id)) is not None
            }
            results = await asyncio.gather(
                *(
                    self.runtime.session_service.cancel_session_run(row.id)
                    for row in pending
                )
            )
            if not all(results):
                raise AgentError("任务停止超时，请重新读取状态后重试。", 503)
            if tasks:
                _, unfinished = await asyncio.wait(tasks, timeout=10)
                if unfinished:
                    raise AgentError("任务停止超时，请重新读取状态后重试。", 503)
            try:
                async with asyncio.timeout(10):
                    while any(
                        await asyncio.gather(
                            *(
                                self.runtime.message_bus.registry_getall(
                                    MessageBusKeys.bg_tasks(session_id)
                                )
                                for session_id in session_ids
                            )
                        )
                    ):
                        await asyncio.sleep(0.05)
            except TimeoutError:
                raise AgentError(
                    "后台工具停止超时，请重新读取状态后重试。", 503
                ) from None
            stopped.update(row.id for row in pending)
            # 负责人 Agent 停止后重新读取团队，覆盖执行期间加入的成员 Agent。
            rows = await self.teams.scope(user_id, root)
            pending = [row for row in rows if row.id not in stopped]
        for original in rows:
            row = await self.storage.get_session(
                user_id, original.agent_id, original.id
            )
            if row is None:
                continue
            status = await self.runtime.session_service.get_session_status(
                user_id, row.agent_id, row.id
            )
            if status in {
                SessionStatus.AWAITING_PERMISSION,
                SessionStatus.AWAITING_EXTERNAL_RESULT,
            }:
                task = self._spawn(
                    user_id, row, UserInterruptEvent(reply_id=row.state.reply_id)
                )
                await asyncio.wait_for(task, timeout=10)
                row = await self.storage.get_session(user_id, row.agent_id, row.id)
                if row is None:
                    continue
            row.state.middle_context[CONTROL_KEY] = intent
            await self.storage.update_session_state(
                user_id, row.agent_id, row.id, row.state
            )
        latest_root = await self.storage.get_session(user_id, root.agent_id, root.id)
        if intent == "cancelled" and latest_root is not None and latest_root.team_id:
            await self.runtime.session_service.delete_team(user_id, latest_root.team_id)
        # 工具退出会触发框架投递收尾提示，等待这些唤醒保存上下文。
        while tasks := {
            task
            for row in rows
            if (task := self.runtime.chat_run_registry.get(row.id)) is not None
            and not task.done()
        }:
            _, unfinished = await asyncio.wait(tasks, timeout=10)
            if unfinished:
                raise AgentError("任务停止超时，请重新读取状态后重试。", 503)
        return {"ok": True}

    async def confirm(
        self,
        user_id: str,
        root: SessionRecord,
        event: UserConfirmResultEvent,
        clients: dict[str, Any],
    ) -> ChatTriggerResponse:
        """校验原生确认事件的归属，使用持久化调用信息恢复对应 Agent 会话。"""
        if root.state.middle_context.get(CONTROL_KEY) in {"interrupted", "cancelled"}:
            raise AgentError("请先恢复已中断的任务。", 409)
        for row in await self.teams.scope(user_id, root):
            if row.state.reply_id != event.reply_id:
                continue
            status = await self.runtime.session_service.get_session_status(
                user_id, row.agent_id, row.id
            )
            task = self.runtime.chat_run_registry.get(row.id)
            if status != SessionStatus.AWAITING_PERMISSION or (
                task is not None and not task.done()
            ):
                raise AgentError("工具确认已失效或正在处理。", 409)
            agent = await self.storage.get_agent(user_id, row.agent_id)
            if agent is None:
                break
            calls = {
                call.id: call
                for call in row.state.get_awaiting_tool_calls(agent.data.name)
                if call.state == ToolCallState.ASKING
            }
            ids = [result.tool_call.id for result in event.confirm_results]
            if not ids or len(ids) != len(set(ids)) or set(ids) - calls.keys():
                raise AgentError("工具确认已失效或不属于当前任务。", 409)
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
            self.contexts.bind(user_id, root.id, clients)
            self._spawn(user_id, row, event)
            return ChatTriggerResponse(session_id=root.id)
        raise AgentError("工具确认已失效或不属于当前任务。", 409)
