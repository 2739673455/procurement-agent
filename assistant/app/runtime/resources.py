"""角色资源装配与能力限制，复用框架工作空间和 Toolkit。"""

from collections.abc import AsyncGenerator, Callable
from typing import Any

from agentscope.agent import Agent
from agentscope.app._tool import AgentInvite
from agentscope.app.message_bus import MessageBus
from agentscope.app.storage import AgentRecord, StorageBase
from agentscope.app.workspace_manager import WorkspaceManagerBase
from agentscope.event import AgentEvent
from agentscope.mcp import MCPClient
from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolBase
from agentscope.workspace import WorkspaceBase

from .catalog import AgentCatalog
from .config import AgentDefinition


async def equip_resources(
    catalog: AgentCatalog,
    workspace: WorkspaceBase,
    agent_id: str,
    session_id: str,
    definition: AgentDefinition,
) -> None:
    """按角色装配资源，安装记录、连接和操作锁由框架管理。"""
    # _declared_specs 只读取配置声明；list_mcps 会建立 MCP 连接。
    reusable_declarations = {
        client.name: client
        for client in workspace._declared_specs(agent_id, session_id)
    }
    desired = {name: catalog.mcps[name] for name in definition.mcps}
    # 先移除过期或配置变化的声明，剩余声明可继续复用。
    for name, client in list(reusable_declarations.items()):
        if name not in desired or client.model_dump() != desired[name].model_dump():
            await workspace.remove_mcp(name, agent_id=agent_id, session_id=session_id)
            del reusable_declarations[name]
    # 全部移除完成后，按角色声明顺序补齐缺失的 MCP。
    for name, client in desired.items():
        if name not in reusable_declarations:
            await workspace.add_mcp(
                MCPClient.model_validate(client.model_dump()),
                agent_id=agent_id,
                session_id=session_id,
            )
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
        """按角色筛选工具、Skill、MCP 和 AgentInvite 的可邀请成员。"""
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
