"""角色资源装配与运行意图中间件，复用框架工作空间和 Toolkit。"""

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any
from weakref import WeakKeyDictionary

from agentscope.agent import Agent
from agentscope.app._tool import AgentInvite
from agentscope.app.message_bus import MessageBus, MessageBusKeys
from agentscope.app.storage import AgentRecord, StorageBase
from agentscope.app.workspace_manager import WorkspaceManagerBase
from agentscope.event import AgentEvent, UserInterruptEvent
from agentscope.mcp import MCPClient
from agentscope.message import HintBlock, Msg
from agentscope.middleware import MiddlewareBase
from agentscope.workspace import WorkspaceBase

from app.config.agents import AgentDefinition
from app.config.loader import ROOT_DIR
from app.runtime.catalog import AgentCatalog
from app.runtime.context import CONTROL_KEY, ContextRegistry, RunContext


class AgentResources:
    """将角色的 Skill 与 MCP 配置装配到框架的 Agent／会话资源分区。"""

    def __init__(self, catalog: AgentCatalog) -> None:
        """保存角色目录及资源装配锁，避免并发成员 Agent 重复安装 Skill。"""
        self.catalog = catalog
        self.locks: WeakKeyDictionary[WorkspaceBase, dict[str, asyncio.Lock]] = (
            WeakKeyDictionary()
        )
        self.configured: WeakKeyDictionary[WorkspaceBase, set[tuple[str, str]]] = (
            WeakKeyDictionary()
        )

    async def equip(
        self,
        workspace: WorkspaceBase,
        agent_id: str,
        session_id: str,
        definition: AgentDefinition,
        *,
        active: bool = True,
    ) -> None:
        """使用框架接口复制完整 Skill 目录，并为会话登记原生 MCP 客户端。"""
        locks = self.locks.setdefault(workspace, {})
        async with locks.setdefault(agent_id, asyncio.Lock()):
            mcp_key = (agent_id, session_id)
            configured = self.configured.setdefault(workspace, set())
            if not active:
                # 中断收尾只补齐工具结果，无需重新连接 MCP。
                for name in definition.mcps:
                    await workspace.remove_mcp(
                        name, agent_id=agent_id, session_id=session_id
                    )
                configured.discard(mcp_key)
                return
            skills = await workspace.list_skills(agent_id=agent_id)
            directories = {
                workspace.get_backend().basename(skill.dir) for skill in skills
            }
            for name in definition.skills:
                if name not in directories:
                    await workspace.add_skill(
                        str(ROOT_DIR / "resources" / "skills" / name), agent_id=agent_id
                    )
            if mcp_key not in configured:
                for client in await workspace.list_mcps(
                    agent_id=agent_id, session_id=session_id
                ):
                    await workspace.remove_mcp(
                        client.name, agent_id=agent_id, session_id=session_id
                    )
                for name in definition.mcps:
                    await workspace.add_mcp(
                        MCPClient.model_validate(
                            self.catalog.mcps.servers[name].model_dump()
                        ),
                        agent_id=agent_id,
                        session_id=session_id,
                    )
                configured.add(mcp_key)


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
        business_tools: set[str],
    ) -> None:
        """保存角色能力以及构造框架 AgentInvite 工具所需的资源。"""
        self.definition = definition
        self.catalog = catalog
        self.storage = storage
        self.message_bus = message_bus
        self.workspace_manager = workspace_manager
        self.user_id = user_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.business_tools = business_tools

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[AgentEvent | Msg]],
    ) -> AsyncGenerator[AgentEvent | Msg]:
        """在执行前收敛能力范围；工具执行与权限判断仍由框架负责。"""
        allowed = (
            set(self.definition.builtin_tools)
            | self.business_tools
            | {"TaskCreate", "TaskList", "TaskGet", "TaskUpdate", "TeamSay", "ToolStop"}
        )
        if self.definition.members:
            allowed |= {"TeamCreate", "TeamDelete", "AgentInvite"}
        pool: list[AgentRecord] = []
        for key in self.definition.members:
            member = await self.storage.get_agent(
                self.user_id, self.catalog.agent_id(self.user_id, key)
            )
            if member is not None:
                pool.append(member)
        for group in agent.toolkit.tool_groups:
            group.tools = [tool for tool in group.tools if tool.name in allowed]
            group.skills_or_loaders = [
                skill
                for skill in group.skills_or_loaders
                if getattr(skill, "name", None) in self.definition.skills
            ]
            group.mcps = [
                client for client in group.mcps if client.name in self.definition.mcps
            ]
            for index, tool in enumerate(group.tools):
                if isinstance(tool, AgentInvite) and pool:
                    group.tools[index] = AgentInvite(
                        self.storage,
                        self.message_bus,
                        self.workspace_manager,
                        self.user_id,
                        self.session_id,
                        self.agent_id,
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
        self.contexts = contexts
        self.context = context
        self.message_bus = message_bus

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[AgentEvent | Msg]],
    ) -> AsyncGenerator[AgentEvent | Msg]:
        """允许显式中断事件收尾；被阻止的唤醒只保存队列提示，不调用模型。"""
        intent = self.contexts.intents.get(
            self.context.root_session_id, self.context.intent
        )
        agent.state.middle_context[CONTROL_KEY] = intent
        if intent != "active" and not isinstance(
            input_kwargs.get("inputs"), UserInterruptEvent
        ):
            while entries := await self.message_bus.queue_drain(
                MessageBusKeys.inbox(agent.state.session_id), max_count=100
            ):
                agent.state.append_context(
                    agent.name,
                    [HintBlock.model_validate(payload) for _, payload in entries],
                )
            return
        async for event in next_handler(**input_kwargs):
            yield event
