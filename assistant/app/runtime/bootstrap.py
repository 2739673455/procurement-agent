"""装配通用 AgentScope 服务，所有触发路径共享角色能力与任务资源管理。"""

from contextlib import asynccontextmanager
from contextvars import ContextVar

from agentscope.agent import Agent, InjectionConfig
from agentscope.app import create_app
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.middleware import ToolOffloadMiddleware
from agentscope.app.workspace_manager import (
    DockerWorkspaceManager,
    IsolationPolicy,
    WorkspaceManagerBase,
)
from agentscope.middleware import MiddlewareBase

from app.config import app_config
from app.config.app_config import ROOT_DIR
from app.runtime.catalog import AgentCatalog
from app.runtime.context import ContextRegistry
from app.runtime.models import ChatCredential, ModelClients
from app.runtime.resources import AgentResources, RoleMiddleware, RunControlMiddleware
from app.runtime.workspaces import session_directory_middlewares


class RuntimeAgent(Agent):
    """根据角色运行策略装配框架 Agent，不改变其推理和消息协议。"""

    def __init__(self, **kwargs):
        """设置提示词时区，并按角色决定工具能否自动转为后台任务。"""
        role = next(
            mw for mw in kwargs["middlewares"] if isinstance(mw, RoleMiddleware)
        )
        if not role.definition.tool_offload:
            kwargs["middlewares"] = [
                mw
                for mw in kwargs["middlewares"]
                if not isinstance(mw, ToolOffloadMiddleware)
            ]
        super().__init__(
            injection_config=InjectionConfig(timezone=app_config.cfg.runtime.timezone),
            **kwargs,
        )


def create_runtime(
    storage,
    *,
    workspace_manager: WorkspaceManagerBase | None = None,
    catalog: AgentCatalog,
):
    """通过框架生命周期装配存储、工作空间、角色和运行资源。"""
    contexts = ContextRegistry(storage, app_config.cfg.runtime.context_ttl_seconds)
    model_clients = ModelClients()
    resources = AgentResources(catalog)
    role_tools: ContextVar[list | None] = ContextVar("role_tools", default=None)
    if workspace_manager is None:
        settings = app_config.cfg.workspace
        workspace_manager = DockerWorkspaceManager(
            str(ROOT_DIR / "data" / "workspaces"),
            isolation=IsolationPolicy.PER_USER,
            base_image=settings.base_image,
            node_version=settings.node_version,
            extra_pip=settings.extra_pip,
            ttl=settings.ttl_seconds,
            sweep_interval=settings.sweep_interval_seconds,
        )

    async def tools(user_id, agent_id, session_id):
        """按角色与认证任务上下文创建业务工具，团队成员使用同一解析路径。"""
        _, definition = catalog.definition(user_id, agent_id)
        context = await contexts.resolve(user_id, session_id)
        cached = role_tools.get()
        if cached is None:
            cached = (
                []
                if context.intent != "active"
                else [
                    tool
                    for key in definition.tool_factories
                    for tool in catalog.factories[key](context)
                ]
            )
            role_tools.set(cached)
        return cached

    async def middlewares(
        user_id, agent_id, session_id, workspace
    ) -> list[MiddlewareBase]:
        """为所有框架运行装配资源、角色能力和会话目录。"""
        model_clients.bind_task()
        role_tools.set(None)
        _, definition = catalog.definition(user_id, agent_id)
        context = await contexts.resolve(user_id, session_id)
        await resources.equip(
            workspace,
            agent_id,
            session_id,
            definition,
            active=context.intent == "active",
        )
        business_tools = {
            tool.name for tool in await tools(user_id, agent_id, session_id)
        }
        return [
            RunControlMiddleware(contexts, context, runtime.state.message_bus),
            RoleMiddleware(
                definition,
                catalog=catalog,
                storage=storage,
                message_bus=runtime.state.message_bus,
                workspace_manager=workspace_manager,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                business_tools=business_tools,
            ),
            *await session_directory_middlewares(
                user_id, agent_id, session_id, workspace
            ),
        ]

    runtime = create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=workspace_manager,
        extra_credentials=[ChatCredential],
        extra_agent_middlewares=middlewares,
        extra_agent_tools=tools,
        custom_agent_cls=RuntimeAgent,
        enable_scheduler=False,
        enable_channel_worker=False,
        enable_index_worker=False,
    )
    native_lifespan = runtime.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        """等待框架运行任务退出后释放所有任务客户端及认证上下文。"""
        try:
            async with native_lifespan(app):
                app.state.catalog = catalog
                app.state.contexts = contexts
                app.state.model_clients = model_clients
                yield
        finally:
            await model_clients.close()
            contexts.contexts.clear()
            contexts.intents.clear()

    runtime.router.lifespan_context = lifespan
    return runtime
