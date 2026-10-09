"""角色资源装配与运行意图中间件，复用框架工作空间和 Toolkit。"""

import asyncio
from weakref import WeakKeyDictionary

from agentscope.app._tool import AgentInvite
from agentscope.app.message_bus import MessageBusKeys
from agentscope.event import UserInterruptEvent
from agentscope.mcp import MCPClient
from agentscope.message import HintBlock
from agentscope.middleware import MiddlewareBase

from app.config.app_config import ROOT_DIR
from app.runtime.context import CONTROL_KEY


class AgentResources:
    """将角色的 Skill 与 MCP 配置装配到框架的 Agent／会话资源分区。"""

    def __init__(self, catalog):
        """保存角色目录及资源装配锁，避免并发成员重复安装 Skill。"""
        self.catalog = catalog
        self.locks: WeakKeyDictionary = WeakKeyDictionary()
        self.configured: WeakKeyDictionary = WeakKeyDictionary()

    async def equip(self, workspace, agent_id, session_id, definition, *, active=True):
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
    """按角色限制工具和资源，并使用原生邀请工具组织允许的团队成员。"""

    def __init__(
        self,
        definition,
        *,
        catalog,
        storage,
        message_bus,
        workspace_manager,
        user_id,
        agent_id,
        session_id,
        business_tools,
    ):
        """保存角色能力以及构造框架邀请工具所需的资源。"""
        self.definition = definition
        self.catalog = catalog
        self.storage = storage
        self.message_bus = message_bus
        self.workspace_manager = workspace_manager
        self.user_id = user_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.business_tools = business_tools

    async def on_reply(self, agent, input_kwargs, next_handler):
        """在执行前收敛能力范围；工具执行与权限判断仍由框架负责。"""
        allowed = (
            set(self.definition.builtin_tools)
            | self.business_tools
            | {"TaskCreate", "TaskList", "TaskGet", "TaskUpdate", "TeamSay"}
        )
        if self.definition.tool_offload:
            allowed.add("ToolStop")
        if self.definition.members:
            allowed |= {"TeamCreate", "TeamDelete", "AgentInvite"}
        pool = []
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
    """阻止已中断或取消任务的自动续跑，保留原生团队提示供恢复时使用。"""

    def __init__(self, contexts, context, message_bus):
        """绑定当前任务上下文及框架消息总线。"""
        self.contexts = contexts
        self.context = context
        self.message_bus = message_bus

    async def on_reply(self, agent, input_kwargs, next_handler):
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
