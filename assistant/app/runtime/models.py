"""对框架创建的原生模型应用运行设置，并在任务结束后释放客户端。"""

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from agentscope.model import ChatModelBase

from .config import ModelConfig


class ModelClients:
    """为原生模型应用宿主运行设置，并管理主模型及备用模型的客户端。"""

    def __init__(self) -> None:
        """保存模型退出栈和异步清理任务。"""
        self._stacks: set[AsyncExitStack] = set()
        self._closing: set[asyncio.Task[None]] = set()

    def configure(self, model: ChatModelBase, settings: ModelConfig) -> None:
        """应用模型运行设置，在当前运行任务结束时释放客户端。"""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("模型装配需要运行任务")
        native_model: Any = model
        stack = AsyncExitStack()
        # 延迟读取 client：with_options 成功时关闭新客户端，失败时仍清理原客户端。
        stack.push_async_callback(lambda: native_model.client.close())
        self._stacks.add(stack)
        task.add_done_callback(lambda _: self._finished(stack))

        if settings.client_kwargs:
            native_model.client = native_model.client.with_options(
                **settings.client_kwargs
            )
        native_model.formatter.input_types = (
            ["text/plain", "image/*"] if settings.image_inputs else ["text/plain"]
        )

        model.context_size = settings.context_size
        model.max_retries = 0

    def _finished(self, stack: AsyncExitStack) -> None:
        """在任务完成后启动异步清理，并跟踪至清理完成。"""
        if stack not in self._stacks:
            return
        self._stacks.remove(stack)
        closing = asyncio.create_task(stack.aclose())
        self._closing.add(closing)
        closing.add_done_callback(self._closing.discard)

    async def close(self) -> None:
        """在框架运行任务退出后，等待所有剩余客户端关闭。"""
        await asyncio.gather(
            *(stack.aclose() for stack in self._stacks), *self._closing
        )
        self._stacks.clear()
