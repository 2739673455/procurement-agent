"""角色资源装配与运行意图中间件，复用框架工作空间和 Toolkit。"""

from collections.abc import AsyncGenerator, Callable
from typing import Any

from agentscope.agent import Agent
from agentscope.app._tool import AgentInvite
from agentscope.app.message_bus import MessageBus, MessageBusKeys
from agentscope.app.storage import AgentRecord, StorageBase
from agentscope.app.workspace_manager import WorkspaceManagerBase
from agentscope.event import AgentEvent, UserInterruptEvent
from agentscope.mcp import MCPClient
from agentscope.message import HintBlock, Msg
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolBase
from agentscope.workspace import WorkspaceBase

from .catalog import AgentCatalog
from .config import AgentDefinition
from .context import CONTROL_KEY, ContextRegistry, RunContext


async def equip_resources(
    catalog: AgentCatalog,
    workspace: WorkspaceBase,
    agent_id: str,
    session_id: str,
    definition: AgentDefinition,
    *,
    active: bool = True,
) -> None:
    """按角色装配资源，安装记录、连接和操作锁由框架管理。"""
    # list_mcps 会建立连接；直接读取框架声明，避免中断收尾重连 MCP。
    current = {
        client.name: client
        for client in workspace._declared_specs(agent_id, session_id)
    }
    desired = {name: catalog.mcps[name] for name in definition.mcps} if active else {}
    for name, client in list(current.items()):
        if name not in desired or client.model_dump() != desired[name].model_dump():
            await workspace.remove_mcp(name, agent_id=agent_id, session_id=session_id)
            del current[name]
    for name, client in desired.items():
        if name not in current:
            await workspace.add_mcp(
                MCPClient.model_validate(client.model_dump()),
                agent_id=agent_id,
                session_id=session_id,
            )
    if not active:
        return

    installed = {skill.name for skill in await workspace.list_skills(agent_id=agent_id)}
    for name in definition.skills:
        if name in installed:
            continue
        try:
            await workspace.add_skill(str(catalog.skills_dir / name), agent_id=agent_id)
        except ValueError:
            # Docker 工作空间并发安装同一 Skill 时，后到的安装会报重名。
            if name not in {
                skill.name for skill in await workspace.list_skills(agent_id=agent_id)
            }:
                raise


class RoleMiddleware(MiddlewareBase):
    """按角色限制工具和资源，并使用原生 AgentInvite 工具邀请允许协作的 Agent。"""

    def __init__(
        self,
        definition: AgentDefinition,
        *,
        catalog: AgentCatalog,
        storage: StorageBase,
        message_bus: MessageBus,
        workspace_manager: WorkspaceManagerBase,
        user_id: str,
        agent_id: str,
        session_id: str,
        business_tools: list[ToolBase],
    ) -> None:
        """保存角色能力以及构造框架 AgentInvite 工具所需的资源。"""
        self._definition = definition
        self._catalog = catalog
        self._storage = storage
        self._message_bus = message_bus
        self._workspace_manager = workspace_manager
        self._user_id = user_id
        self._agent_id = agent_id
        self._session_id = session_id
        self._business_tools = business_tools

    async def list_tools(self) -> list[ToolBase]:
        """通过框架中间件接口注册本轮业务工具。"""
        return self._business_tools

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[AgentEvent | Msg]],
    ) -> AsyncGenerator[AgentEvent | Msg]:
        """在执行前收敛能力范围；工具执行与权限判断仍由框架负责。"""
        allowed = (
            set(self._definition.builtin_tools)
            | {tool.name for tool in self._business_tools}
            | {"TaskCreate", "TaskList", "TaskGet", "TaskUpdate", "TeamSay", "ToolStop"}
        )
        if self._definition.members:
            allowed |= {"TeamCreate", "TeamDelete", "AgentInvite"}
        pool: list[AgentRecord] = []
        for key in self._definition.members:
            member = await self._storage.get_agent(
                self._user_id, self._catalog.agent_id(self._user_id, key)
            )
            if member is not None:
                pool.append(member)
        for group in agent.toolkit.tool_groups:
            group.tools = [tool for tool in group.tools if tool.name in allowed]
            group.skills_or_loaders = [
                skill
                for skill in group.skills_or_loaders
                if getattr(skill, "name", None) in self._definition.skills
            ]
            group.mcps = [
                client for client in group.mcps if client.name in self._definition.mcps
            ]
            for index, tool in enumerate(group.tools):
                if isinstance(tool, AgentInvite) and pool:
                    group.tools[index] = AgentInvite(
                        self._storage,
                        self._message_bus,
                        self._workspace_manager,
                        self._user_id,
                        self._session_id,
                        self._agent_id,
                        invitable_pool=pool,
                    )
        async for event in next_handler(**input_kwargs):
            yield event


class RunControlMiddleware(MiddlewareBase):
    """阻止已中断或取消任务的自动续跑，保留原生 Agent 团队提示供恢复时使用。"""

    def __init__(
        self,
        contexts: ContextRegistry,
        context: RunContext,
        message_bus: MessageBus,
    ) -> None:
        """绑定当前任务上下文及框架消息总线。"""
        self._contexts = contexts
        self._context = context
        self._message_bus = message_bus

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[AgentEvent | Msg]],
    ) -> AsyncGenerator[AgentEvent | Msg]:
        """允许显式中断事件收尾；被阻止的唤醒只保存队列提示，不调用模型。"""
        intent = self._contexts.intent(self._context)
        agent.state.middle_context[CONTROL_KEY] = intent
        if intent != "active" and not isinstance(
            input_kwargs.get("inputs"), UserInterruptEvent
        ):
            while entries := await self._message_bus.queue_drain(
                MessageBusKeys.inbox(agent.state.session_id), max_count=100
            ):
                agent.state.append_context(
                    agent.name,
                    [HintBlock.model_validate(payload) for _, payload in entries],
                )
            return
        async for event in next_handler(**input_kwargs):
            yield event
