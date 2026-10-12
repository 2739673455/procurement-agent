"""按用户和团队会话绑定客户端及运行意图，供工具和协作 Agent 使用。"""

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

from agentscope.app.storage import SessionRecord, StorageBase

from .errors import RunError

# 非敏感运行意图保存在框架提供的中间件状态中。
CONTROL_KEY = "runtime.run_control"

# active 允许执行；interrupted 等待用户恢复；cancelled 结束任务。
type RunIntent = Literal["active", "interrupted", "cancelled"]


@dataclass
class RunContext:
    """一次会话运行使用的用户、业务客户端和运行意图。"""

    user_id: str  # 框架内的用户标识，用于检查会话归属。
    root_session_id: str  # 用户主会话 ID；成员 Agent 使用负责人 Agent 会话的 ID。
    clients: dict[str, Any] = field(repr=False)  # 携带用户凭据的业务客户端。
    bound_at: float = field(default_factory=monotonic)  # 绑定时间，用于检查有效期。
    intent: RunIntent = "active"  # 允许运行、中断或取消。


class ContextRegistry:
    """按负责人 Agent 的会话保存上下文，检查用户归属、客户端有效期和运行意图。"""

    def __init__(self, storage: StorageBase, ttl_seconds: float) -> None:
        """保存框架存储引用和客户端有效期。"""
        self._storage = storage
        self._ttl_seconds = ttl_seconds
        self._contexts: dict[str, RunContext] = {}
        self._intents: dict[str, RunIntent] = {}

    def bind(self, user_id: str, root_session_id: str, clients: dict[str, Any]) -> None:
        """保存本次请求提供的用户和业务客户端，并将会话标记为允许运行。"""
        now = monotonic()
        for identifier, context in list(self._contexts.items()):
            if now - context.bound_at > self._ttl_seconds:
                self._contexts.pop(identifier)
        self._contexts[root_session_id] = RunContext(user_id, root_session_id, clients)
        self._intents[root_session_id] = "active"

    def block(self, root_session_id: str, intent: RunIntent) -> None:
        """在内存中标记中断或取消，阻止负责人 Agent 及成员 Agent 自动续跑。"""
        self._intents[root_session_id] = intent

    def forget(self, root_session_id: str) -> None:
        """移除已删除会话的内存上下文和运行意图。"""
        self._contexts.pop(root_session_id, None)
        self._intents.pop(root_session_id, None)

    def intent(self, context: RunContext) -> RunIntent:
        """读取上下文所属任务的最新运行意图。"""
        return self._intents.get(context.root_session_id, context.intent)

    def clear(self) -> None:
        """释放全部内存上下文和运行意图。"""
        self._contexts.clear()
        self._intents.clear()

    async def root(self, user_id: str, session_id: str) -> SessionRecord:
        """核实会话归属；如果属于成员 Agent，则返回负责人 Agent 的会话。"""
        row = await self._storage.get_session(user_id, "", session_id)
        if row is None:
            raise RunError("会话不存在或无权访问。", "not_found")
        if row.team_id:
            team = await self._storage.get_team(user_id, row.team_id)
            if team is not None and team.session_id != row.id:
                leader = await self._storage.get_session(user_id, "", team.session_id)
                if leader is None:
                    raise RunError("团队负责人 Agent 的会话不存在。", "not_found")
                return leader
        return row

    async def resolve(self, user_id: str, session_id: str) -> RunContext:
        """取得会话上下文，检查用户和有效期。

        中断或取消时返回不含业务客户端的上下文，供框架收尾。
        """
        root = await self.root(user_id, session_id)
        intent = self._intents.get(
            root.id, root.state.middle_context.get(CONTROL_KEY) or "active"
        )
        context = self._contexts.get(root.id)
        if intent != "active":
            return RunContext(user_id, root.id, {}, intent=intent)
        if (
            context is None
            or context.user_id != user_id
            or monotonic() - context.bound_at > self._ttl_seconds
        ):
            self._contexts.pop(root.id, None)
            raise RunError(
                "运行上下文已失效，请重新绑定客户端后启动或恢复任务。",
                "context_expired",
            )
        return context
