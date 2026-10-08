"""AgentScope 应用装配与 ERPNext 请求上下文绑定。"""

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
from agentscope.tool import ToolBase

from app.agent.model import ChatCredential
from app.agent.tools.items import create_items_tool
from app.agent.workspaces import session_directory_middlewares
from app.clients.erpnext.client import ERPNext
from app.config import app_config
from app.config.app_config import ROOT_DIR
from app.errors.agent import AgentError

# Task 创建时复制请求上下文；凭据不进入存储、消息总线或模型提示词。
erp_context: ContextVar[ERPNext] = ContextVar("erp_context")


async def procurement_tools(user_id, agent_id, session_id) -> list[ToolBase]:
    """框架工具工厂：使用本轮请求上下文中的 ERPNext 登录身份创建工具。"""
    try:
        erp = erp_context.get()
    except LookupError:
        raise AgentError(
            "当前运行缺少 ERPNext 登录身份，请重新发送消息。", 401
        ) from None
    return [create_items_tool(erp)]


class ProcurementAgent(Agent):
    """使用框架工具集和请求登录身份执行任务的采购 Agent。"""

    def __init__(self, **kwargs):
        """设置提示词时区，并禁用工具自动转为后台任务。"""
        # ERP 工具绑定当前登录态，必须随本轮停止，不转为跨轮唤醒的后台工具。
        kwargs["middlewares"] = [
            middleware
            for middleware in kwargs.get("middlewares", [])
            if not isinstance(middleware, ToolOffloadMiddleware)
        ]
        super().__init__(
            injection_config=InjectionConfig(timezone="Asia/Shanghai"),
            **kwargs,
        )

    async def reply_stream(self, *args, **kwargs):
        """转发框架回复事件，并在执行结束或取消时关闭模型客户端。"""
        try:
            async for event in super().reply_stream(*args, **kwargs):
                yield event
        finally:
            client = getattr(self.model, "client", None)
            if client is not None:
                await client.close()


def create_runtime(storage, *, workspace_manager: WorkspaceManagerBase | None = None):
    """使用框架生命周期装配服务；管理端路由不对 ERPNext 用户开放。"""
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
    return create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=workspace_manager,
        extra_credentials=[ChatCredential],
        extra_agent_middlewares=session_directory_middlewares,
        extra_agent_tools=procurement_tools,
        custom_agent_cls=ProcurementAgent,
        enable_scheduler=False,
        enable_channel_worker=False,
        enable_index_worker=False,
    )
