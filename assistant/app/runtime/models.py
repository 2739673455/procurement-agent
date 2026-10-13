"""AgentScope 模型装配；数据库保存配置引用，连接参数由服务端解析。"""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import AsyncGenerator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from typing import Any, Literal

from agentscope.credential import CredentialBase
from agentscope.formatter import FormatterBase
from agentscope.model import ChatModelBase
from pydantic import BaseModel

from .config import ModelConfig
from .errors import RunError

# 每个运行任务持有独立的退出栈，统一释放本次装配的模型客户端。
_model_clients: ContextVar[AsyncExitStack | None] = ContextVar(
    "model_clients", default=None
)
_model_settings: ContextVar[Mapping[str, ModelConfig]] = ContextVar("model_settings")


class ModelClients:
    """覆盖框架独立唤醒任务，在任务结束或服务关闭时释放模型客户端。"""

    def __init__(self, settings: Mapping[str, ModelConfig]) -> None:
        """保存任务退出栈及正在执行的异步释放操作。"""
        self._settings = settings
        self._stacks: dict[asyncio.Task[Any], AsyncExitStack] = {}
        self._closing: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def scope(self) -> AsyncGenerator[None]:
        """为本次执行绑定模型配置，并在结束、失败或取消后关闭客户端。"""
        async with AsyncExitStack() as stack:
            settings_token = _model_settings.set(self._settings)
            clients_token = _model_clients.set(stack)
            try:
                yield
            finally:
                _model_clients.reset(clients_token)
                _model_settings.reset(settings_token)

    def bind_task(self) -> None:
        """为没有显式资源作用域的当前框架任务登记退出栈。"""
        _model_settings.set(self._settings)
        if _model_clients.get() is not None:
            return
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("模型装配需要运行任务")
        stack = self._stacks.setdefault(task, AsyncExitStack())
        _model_clients.set(stack)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[Any]) -> None:
        """在任务完成后启动异步清理，并跟踪至清理完成。"""
        stack = self._stacks.pop(task, None)
        if stack is not None:
            closing = asyncio.create_task(stack.aclose())
            self._closing.add(closing)
            closing.add_done_callback(self._closing.discard)

    async def close(self) -> None:
        """在框架运行任务退出后，等待所有剩余客户端关闭。"""
        await asyncio.gather(*(stack.aclose() for stack in self._stacks.values()))
        self._stacks.clear()
        if self._closing:
            await asyncio.gather(*self._closing)


class ChatCredential(CredentialBase):
    """引用服务端配置，供框架装配提供商原生模型。"""

    # 框架通过此类型标识识别和还原凭据。
    type: Literal["configured_chat"] = "configured_chat"
    config_key: str  # 服务端模型配置集合中的条目名称。

    @property
    def settings(self) -> ModelConfig:
        """按配置引用读取模型设置，引用不存在时抛出配置错误。"""
        settings = _model_settings.get().get(self.config_key)
        # 数据库中的配置引用必须对应服务端模型配置中的条目。
        if settings is None:
            raise RunError(f"模型配置引用不存在：{self.config_key}", "unavailable")
        return settings

    @classmethod
    def get_chat_model_class(cls) -> builtins.type[ConfiguredChatModel]:
        """向框架凭据工厂提供要装配的模型类。"""
        return ConfiguredChatModel


class ConfiguredChatModel(ChatModelBase):
    """框架的配置引用装配入口；构造结果是提供商原生模型实例。"""

    def __new__(
        cls,
        credential: ChatCredential,
        model: str,
        parameters: BaseModel | None = None,
    ) -> ChatModelBase:
        """从服务端配置创建模型，推理、格式化及请求参数处理由原生模型负责。"""
        settings = credential.settings
        model_cls = settings.credential.get_chat_model_class()
        kwargs: dict[str, Any] = {
            "credential": settings.credential,
            "model": settings.model,
            "parameters": model_cls.Parameters(**settings.params),
            "context_size": settings.context_size,
            "max_retries": 0,
            "client_kwargs": settings.client_kwargs,
        }
        native_model: Any = model_cls(**kwargs)
        formatter: FormatterBase = native_model.formatter
        formatter.input_types = (
            ["text/plain", "image/*"] if settings.image_inputs else ["text/plain"]
        )
        stack = _model_clients.get()
        client = getattr(native_model, "client", None)
        if stack is not None and client is not None:
            stack.push_async_callback(client.close)
        return native_model
