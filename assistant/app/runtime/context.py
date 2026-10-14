"""按用户和团队会话绑定业务客户端，供工具和协作 Agent 使用。"""

from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from agentscope.app.storage import SessionRecord, StorageBase

from .errors import RunError


@dataclass
class RunContext:
    """负责人会话及其成员共享的用户身份和业务客户端绑定。"""

    user_id: str  # 框架内的用户标识，用于检查会话归属。
    root_session_id: str  # 用户主会话 ID；成员 Agent 使用负责人 Agent 会话的 ID。
    clients: dict[str, Any] = field(repr=False)  # 携带用户凭据的业务客户端。
    bound_at: float = field(default_factory=monotonic)  # 单调时钟读数，用于检查有效期。


class ContextRegistry:
    """按负责人会话保存客户端绑定，检查用户归属及绑定有效期。"""

    def __init__(self, storage: StorageBase, ttl_seconds: float) -> None:
        """保存框架存储引用和客户端绑定有效期。"""
        self._storage = storage
        self._ttl_seconds = ttl_seconds
        self._contexts: dict[str, RunContext] = {}

    def bind(self, user_id: str, root_session_id: str, clients: dict[str, Any]) -> None:
        """清理过期绑定，保存本次请求的用户和业务客户端。"""
        now = monotonic()
        for identifier, context in list(self._contexts.items()):
            if now - context.bound_at > self._ttl_seconds:
                self._contexts.pop(identifier)
        self._contexts[root_session_id] = RunContext(user_id, root_session_id, clients)

    def forget(self, root_session_id: str) -> None:
        """移除已删除会话的内存上下文。"""
        self._contexts.pop(root_session_id, None)

    def clear(self) -> None:
        """清除全部客户端绑定，客户端的关闭由宿主负责。"""
        self._contexts.clear()

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
        """取得会话上下文，检查用户和有效期。"""
        root = await self.root(user_id, session_id)
        context = self._contexts.get(root.id)
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
