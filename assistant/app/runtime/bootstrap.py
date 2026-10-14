"""装配通用 AgentScope 服务，所有触发路径共享 Agent 角色能力与任务资源管理。"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from agentscope.agent import Agent, InjectionConfig
from agentscope.app import create_app
from agentscope.app.message_bus import MessageBus
from agentscope.app.middleware import ToolOffloadMiddleware
from agentscope.app.storage import StorageBase
from agentscope.app.workspace_manager import WorkspaceManagerBase
from agentscope.middleware import MiddlewareBase
from agentscope.workspace import WorkspaceBase
from fastapi import FastAPI

from .agents import AgentRegistry
from .catalog import AgentCatalog
from .context import ContextRegistry
from .models import ModelClients
from .offloading import ToolExecutionMiddleware
from .resources import RoleMiddleware, equip_resources
from .runs import RunController
from .runtime import Runtime
from .teams import TeamService
from .workspaces import session_directory_middlewares


def create_runtime(
    storage: StorageBase,
    *,
    message_bus: MessageBus,
    workspace_manager: WorkspaceManagerBase,
    catalog: AgentCatalog,
    injection_config: InjectionConfig,
    context_ttl_seconds: float,
) -> Runtime:
    """通过框架生命周期装配存储、工作空间、角色和运行资源。"""
    contexts = ContextRegistry(storage, context_ttl_seconds)
    model_clients = ModelClients()

    class RuntimeAgent(Agent):
        """接入模型客户端释放、注入配置和工具取消收尾。"""

        def __init__(self, **kwargs: Any) -> None:
            """应用模型运行设置，注册客户端释放和工具取消处理。"""
            for model in (kwargs["model"], kwargs["model_config"].fallback_model):
                if model is not None:
                    # 凭据 name 对应模型配置名，用于查找运行设置。
                    settings = catalog.definitions.models[model.credential.name]
                    model_clients.configure(model, settings)
            kwargs["middlewares"] = [
                ToolExecutionMiddleware(mw)
                if isinstance(mw, ToolOffloadMiddleware)
                else mw
                for mw in kwargs["middlewares"]
            ]
            super().__init__(injection_config=injection_config, **kwargs)

    async def middlewares(
        user_id: str, agent_id: str, session_id: str, workspace: WorkspaceBase
    ) -> list[MiddlewareBase]:
        """为所有框架运行装配资源、角色能力和会话目录。"""
        _, definition = catalog.definition(user_id, agent_id)
        context = await contexts.resolve(user_id, session_id)
        await equip_resources(
            catalog,
            workspace,
            agent_id,
            session_id,
            definition,
        )
        business_tools = catalog.create_tools(definition, context)
        return [
            RoleMiddleware(
                definition,
                catalog=catalog,
                storage=storage,
                message_bus=message_bus,
                workspace_manager=workspace_manager,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                business_tools=business_tools,
            ),
            *await session_directory_middlewares(session_id, workspace),
        ]

    runtime = create_app(
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        extra_agent_middlewares=middlewares,
        custom_agent_cls=RuntimeAgent,
        enable_scheduler=False,
        enable_channel_worker=False,
        enable_index_worker=False,
    )
    native_lifespan = runtime.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """由框架管理运行资源，退出后释放模型客户端与绑定上下文。"""
        await catalog.validate_skills()
        try:
            async with native_lifespan(app):
                app.state.catalog = catalog
                app.state.contexts = contexts
                app.state.agents = AgentRegistry(app.state)
                teams = TeamService(app.state)
                app.state.teams = teams
                app.state.runs = RunController(app.state, teams)
                yield
        finally:
            await model_clients.close()
            contexts.clear()

    runtime.router.lifespan_context = lifespan
    return Runtime(runtime, catalog, workspace_manager)
