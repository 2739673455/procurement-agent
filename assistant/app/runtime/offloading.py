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
        self.offload = offload

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk | ToolResponse]],
    ) -> AsyncGenerator[ToolChunk | ToolResponse]:
        """后台转移和结果投递沿用框架；取消时等待内部工具任务退出。"""
        execution: asyncio.Task[Any] | None = None
        collected: list[ToolChunk | ToolResponse] = []

        async def execute(**kwargs: Any) -> AsyncGenerator[ToolChunk | ToolResponse]:
            """记录框架创建的执行任务及其原生结果。"""
            nonlocal execution
            execution = asyncio.current_task()
            async for item in next_handler(**kwargs):
                collected.append(item)
                yield item

        try:
            async for item in self.offload.on_acting(agent, input_kwargs, execute):
                yield item
        except asyncio.CancelledError:
            if execution is None or execution is asyncio.current_task():
                raise
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            # 并发执行器通过 Toolkit 的中断结果结束本轮，避免重复调用工具。
            for item in collected:
                yield item
