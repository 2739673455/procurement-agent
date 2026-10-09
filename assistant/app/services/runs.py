"""通用任务控制；执行、工具收尾、状态保存与事件发布均复用 AgentScope。"""

import asyncio

from agentscope.app._router._schema import ChatTriggerResponse
from agentscope.app._service import SessionStatus
from agentscope.event import UserConfirmResultEvent, UserInterruptEvent
from agentscope.message import ToolCallState
from agentscope.types import ReplyFinishedReason

from app.errors.agent import AgentError
from app.runtime.context import CONTROL_KEY
from app.runtime.models import model_client_scope


class RunService:
    """协调认证上下文、框架后台运行以及整个团队的中断和取消。"""

    def __init__(self, runtime, teams):
        """绑定框架服务、上下文提供器和团队范围解析器。"""
        self.runtime = runtime
        self.storage = runtime.storage
        self.contexts = runtime.contexts
        self.teams = teams

    async def _invoke(self, user_id, row, incoming):
        """保持完整运行的客户端作用域，框架负责推理与持久化。"""
        async with model_client_scope():
            await self.runtime.chat_service.run(user_id, row.id, row.agent_id, incoming)

    def _spawn(self, user_id, row, incoming):
        """通过框架任务注册器启动执行，防止会话重复运行。"""
        coroutine = self._invoke(user_id, row, incoming)
        try:
            return self.runtime.chat_run_registry.spawn(coroutine, session_id=row.id)
        except RuntimeError:
            coroutine.close()
            raise AgentError("当前会话正在执行。", 409) from None

    async def require_idle(self, user_id, row):
        """使用框架状态及待启动任务检查整个任务范围是否空闲。"""
        if (await self.teams.summary(user_id, row))["running"]:
            raise AgentError("当前任务正在执行。", 409)

    async def _activate(self, user_id, root, clients):
        """绑定新认证身份并持久清除团队的中断意图。"""
        self.contexts.bind(user_id, root.id, clients)
        for row in await self.teams.scope(user_id, root):
            row.state.middle_context[CONTROL_KEY] = "active"
            await self.storage.update_session_state(
                user_id, row.agent_id, row.id, row.state
            )

    async def start(self, user_id, root, incoming, clients):
        """启动新的用户输入，保留同一会话已经保存的对话上下文。"""
        await self.require_idle(user_id, root)
        summary = await self.teams.summary(user_id, root)
        if summary["confirmations"]:
            raise AgentError("请先处理工具确认，或取消当前任务。", 409)
        await self._activate(user_id, root, clients)
        self._spawn(user_id, root, incoming)
        return ChatTriggerResponse(session_id=root.id)

    async def resumable(self, user_id, root):
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

    async def resume(self, user_id, root, clients):
        """加载已保存的上下文继续执行，并恢复中断的团队成员。"""
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

    async def stop(self, user_id, root, clients, *, intent):
        """停止负责人及成员，等待框架保存状态；取消同时解散当前团队。"""
        self.contexts.bind(user_id, root.id, clients)
        self.contexts.block(root.id, intent)
        rows = await self.teams.scope(user_id, root)
        pending = rows
        stopped = set()
        while pending:
            tasks = [self.runtime.chat_run_registry.get(row.id) for row in pending]
            results = await asyncio.gather(
                *(
                    self.runtime.session_service.cancel_session_run(row.id)
                    for row in pending
                )
            )
            if not all(results):
                raise AgentError("任务停止超时，请重新读取状态后重试。", 503)
            await asyncio.wait_for(
                asyncio.gather(
                    *(task for task in tasks if task is not None),
                    return_exceptions=True,
                ),
                timeout=10,
            )
            stopped.update(row.id for row in pending)
            # 负责人停止后重新读取团队，覆盖执行期间加入的成员。
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
        root = await self.storage.get_session(user_id, root.agent_id, root.id)
        if intent == "cancelled" and root is not None and root.team_id:
            await self.runtime.session_service.delete_team(user_id, root.team_id)
        return {"ok": True}

    async def confirm(self, user_id, root, event: UserConfirmResultEvent, clients):
        """校验原生确认事件的归属，使用持久化调用信息恢复正确的成员会话。"""
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
