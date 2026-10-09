"""AgentScope 模型装配；数据库保存配置引用，连接参数由服务端解析。"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from typing import Literal

from agentscope.credential import CredentialBase, OpenAICredential
from agentscope.formatter import DeepSeekChatFormatter
from agentscope.model import OpenAIChatModel

from app.config import app_config
from app.errors.agent import AgentError

# 每个运行任务持有独立的退出栈，统一释放本次装配的模型客户端。
_model_clients: ContextVar[AsyncExitStack | None] = ContextVar(
    "model_clients", default=None
)


@asynccontextmanager
async def model_client_scope() -> AsyncGenerator[None]:
    """在整个运行结束、失败或取消后，关闭期间创建的模型客户端。"""
    async with AsyncExitStack() as stack:
        token = _model_clients.set(stack)
        try:
            yield
        finally:
            _model_clients.reset(token)


def register_model_client(client) -> None:
    """将客户端登记到当前运行；独立创建时由调用方负责关闭。"""
    stack = _model_clients.get()
    if stack is not None:
        stack.push_async_callback(client.close)


class ModelClients:
    """覆盖框架独立唤醒任务，在任务结束或服务关闭时释放模型客户端。"""

    def __init__(self):
        """保存任务退出栈及正在执行的异步释放操作。"""
        self.stacks: dict[asyncio.Task, AsyncExitStack] = {}
        self.closing: set[asyncio.Task] = set()

    def bind_task(self) -> None:
        """为没有显式资源作用域的当前框架任务登记退出栈。"""
        if _model_clients.get() is not None:
            return
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("模型装配需要运行任务")
        stack = self.stacks.setdefault(task, AsyncExitStack())
        _model_clients.set(stack)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        """在任务完成后启动异步清理，并跟踪至清理完成。"""
        stack = self.stacks.pop(task, None)
        if stack is not None:
            closing = asyncio.create_task(stack.aclose())
            self.closing.add(closing)
            closing.add_done_callback(self.closing.discard)

    async def close(self) -> None:
        """在框架运行任务退出后，等待所有剩余客户端关闭。"""
        await asyncio.gather(*(stack.aclose() for stack in self.stacks.values()))
        self.stacks.clear()
        if self.closing:
            await asyncio.gather(*self.closing)


class ChatCredential(CredentialBase):
    """引用服务端配置，供框架装配 Chat Completions 模型。"""

    # 框架通过此类型标识识别和还原凭据。
    type: Literal["configured_chat"] = "configured_chat"
    config_key: str  # 服务端模型配置集合中的条目名称。

    @property
    def settings(self):
        """按配置引用读取模型设置，引用不存在时抛出配置错误。"""
        settings = app_config.cfg.lm_config.models.get(self.config_key)
        # 数据库中的配置引用必须对应服务端模型配置中的条目。
        if settings is None:
            raise AgentError("请检查配置文件中的模型配置。", 503)
        return settings

    @classmethod
    def get_chat_model_class(cls):
        """向框架凭据工厂提供要装配的模型类。"""
        return ChatCompletionsModel


class ChatCompletionsModel(OpenAIChatModel):
    """将服务端配置和服务商消息格式注入框架模型。"""

    def __init__(self, credential: ChatCredential, model: str, parameters=None):
        """从凭据引用解析连接参数，并配置服务商消息格式与请求参数。"""
        settings = credential.settings
        formatter = None
        if settings.model_provider == "deepseek":
            formatter = DeepSeekChatFormatter(
                input_types=["text/plain", "image/*"]
                if settings.image_inputs
                else ["text/plain"]
            )
        super().__init__(
            credential=OpenAICredential(
                api_key=settings.api_key,
                base_url=settings.base_url,
            ),
            model=settings.model,
            context_size=settings.context_size or 32768,
            max_retries=0,
            client_kwargs={"timeout": settings.timeout_seconds, "max_retries": 0},
            extra_body=settings.params,
            formatter=formatter,
        )
        register_model_client(self.client)
