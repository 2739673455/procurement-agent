"""按认证任务绑定业务客户端，并通过团队归属供独立框架任务读取。"""

from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from app.errors.agent import AgentError

# 非敏感运行意图保存在框架提供的中间件状态中。
CONTROL_KEY = "app.run_control"


@dataclass
class RunContext:
    """任务的认证归属与业务客户端；客户端只保留在服务端内存中。"""

    user_id: str
    root_session_id: str
    clients: dict[str, Any] = field(repr=False)
    bound_at: float = field(default_factory=monotonic)
    intent: str = "active"


class ContextRegistry:
    """将会话及团队成员映射到已认证的任务上下文，限制凭据使用时长。"""

    def __init__(self, storage, ttl_seconds: float):
        """绑定框架存储及客户端有效期，不将客户端序列化到存储。"""
        self.storage = storage
        self.ttl_seconds = ttl_seconds
        self.contexts: dict[str, RunContext] = {}
        self.intents: dict[str, str] = {}

    def bind(self, user_id: str, root_session_id: str, clients: dict) -> None:
        """将经过身份核验的客户端绑定到当前任务，开始新的执行阶段。"""
        now = monotonic()
        for identifier, context in list(self.contexts.items()):
            if now - context.bound_at > self.ttl_seconds:
                self.contexts.pop(identifier)
        self.contexts[root_session_id] = RunContext(user_id, root_session_id, clients)
        self.intents[root_session_id] = "active"

    def block(self, root_session_id: str, intent: str) -> None:
        """立即阻止任务的后续自动唤醒，持久意图由运行服务保存。"""
        self.intents[root_session_id] = intent

    def forget(self, root_session_id: str) -> None:
        """释放已删除会话的客户端与运行意图。"""
        self.contexts.pop(root_session_id, None)
        self.intents.pop(root_session_id, None)

    async def root(self, user_id: str, session_id: str):
        """核实会话归属，团队成员使用团队负责人的任务上下文。"""
        row = await self.storage.get_session(user_id, "", session_id)
        if row is None:
            raise AgentError("会话不存在或无权访问。", 404)
        if row.team_id:
            team = await self.storage.get_team(user_id, row.team_id)
            if team is not None and team.session_id != row.id:
                leader = await self.storage.get_session(user_id, "", team.session_id)
                if leader is None:
                    raise AgentError("团队负责人会话不存在。", 404)
                return leader
        return row

    async def resolve(self, user_id: str, session_id: str) -> RunContext:
        """为框架触发路径取得认证上下文；阻止任务可在没有凭据时静默收尾。"""
        root = await self.root(user_id, session_id)
        intent = self.intents.get(
            root.id, root.state.middle_context.get(CONTROL_KEY) or "active"
        )
        context = self.contexts.get(root.id)
        if intent != "active":
            return RunContext(user_id, root.id, {}, intent=intent)
        if (
            context is None
            or context.user_id != user_id
            or monotonic() - context.bound_at > self.ttl_seconds
        ):
            self.contexts.pop(root.id, None)
            raise AgentError("任务登录上下文已失效，请重新发送消息或恢复任务。", 401)
        return context
