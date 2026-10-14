"""工具后台执行由框架负责，中断时等待执行任务并保留 Toolkit 的收尾结果。"""

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any

from agentscope.agent import Agent
from agentscope.app.middleware import ToolOffloadMiddleware
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolChunk, ToolResponse


class ToolExecutionMiddleware(MiddlewareBase):
    """让框架内部的工具执行任务收到中断，并转发 Toolkit 的中断结果。"""

    def __init__(self, offload: ToolOffloadMiddleware) -> None:
        """使用框架创建的后台执行中间件。"""
        self._offload = offload

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk | ToolResponse]],
    ) -> AsyncGenerator[ToolChunk | ToolResponse]:
        """通过框架转移后台任务和投递结果，取消时等待内部工具任务退出。"""
        tool_task: asyncio.Task[Any] | None = None
        captured_results: list[ToolChunk | ToolResponse] = []

        async def execute_and_capture(
            **kwargs: Any,
        ) -> AsyncGenerator[ToolChunk | ToolResponse]:
            """记录框架创建的执行任务及其原生结果。"""
            nonlocal tool_task
            tool_task = asyncio.current_task()
            async for item in next_handler(**kwargs):
                captured_results.append(item)
                yield item

        try:
            async for item in self._offload.on_acting(
                agent, input_kwargs, execute_and_capture
            ):
                yield item
        except asyncio.CancelledError:
            if tool_task is None or tool_task is asyncio.current_task():
                raise
            tool_task.cancel()
            # 等待收尾期间，execute_and_capture 仍会记录 Toolkit 的中断结果。
            await asyncio.gather(tool_task, return_exceptions=True)
            # 将 Toolkit 的中断结果交给并发执行器，标记本轮工具调用结束。
            for item in captured_results:
                yield item
