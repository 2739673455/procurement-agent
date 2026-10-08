"""AgentScope 应用装配与采购工具边界。"""

from contextvars import ContextVar

from agentscope.agent import Agent, InjectionConfig
from agentscope.app import create_app
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.middleware import ToolOffloadMiddleware
from agentscope.app.workspace_manager import IsolationPolicy, LocalWorkspaceManager
from agentscope.tool import ToolBase, Toolkit

from app.agent.model import ChatCredential
from app.agent.tools.items import create_items_tool
from app.clients.erpnext.client import ERPNext
from app.config.app_config import ROOT_DIR
from app.errors.agent import AgentError

# Task 创建时复制请求上下文；凭据不进入存储、消息总线或模型提示词。
erp_context: ContextVar[ERPNext] = ContextVar("erp_context")


async def procurement_tools(user_id, agent_id, session_id) -> list[ToolBase]:
    try:
        erp = erp_context.get()
    except LookupError:
        raise AgentError(
            "当前运行缺少 ERPNext 登录身份，请重新发送消息。", 401
        ) from None
    return [create_items_tool(erp)]


class ProcurementAgent(Agent):
    def __init__(self, *, toolkit, **kwargs):
        # 从框架装配的工具集中仅开放物料查询。
        tools = [
            tool
            for group in toolkit.tool_groups
            for tool in group.tools
            if tool.name == "query_items"
        ]
        # 未开放文件读取工具，避免大结果卸载后要求模型读取不可访问的文件。
        kwargs["offloader"] = None
        # ERP 工具绑定当前登录态，必须随本轮停止，不转为跨轮唤醒的后台工具。
        kwargs["middlewares"] = [
            middleware
            for middleware in kwargs.get("middlewares", [])
            if not isinstance(middleware, ToolOffloadMiddleware)
        ]
        super().__init__(
            toolkit=Toolkit(tools=tools),
            injection_config=InjectionConfig(timezone="Asia/Shanghai"),
            **kwargs,
        )

    async def reply_stream(self, *args, **kwargs):
        try:
            async for event in super().reply_stream(*args, **kwargs):
                yield event
        finally:
            client = getattr(self.model, "client", None)
            if client is not None:
                await client.close()


def create_runtime(storage, *, workspace_dir=None):
    """使用框架生命周期装配服务；管理端路由不对 ERPNext 用户开放。"""
    return create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(
            str(workspace_dir or ROOT_DIR / "data" / "workspaces"),
            isolation=IsolationPolicy.PER_SESSION,
        ),
        extra_credentials=[ChatCredential],
        extra_agent_tools=procurement_tools,
        custom_agent_cls=ProcurementAgent,
        enable_scheduler=False,
        enable_channel_worker=False,
        enable_index_worker=False,
    )
