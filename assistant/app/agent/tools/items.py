"""物料查询工具，使用当前请求的 ERPNext 身份执行。"""

import asyncio
from typing import Annotated

from langchain.tools import ToolRuntime, tool
from pydantic import Field

from app.agent.context import TurnContext
from app.clients.erpnext import items
from app.errors.agent import AgentError


@tool
async def query_items(
    query: str,
    runtime: ToolRuntime[TurnContext],
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1)] = 20,
) -> dict:
    """只读查询当前用户可访问的 ERPNext Item。

    query 为物料编码或名称关键词，空字符串列出物料。
    offset 和 limit 控制分页；不提供库存、价格或供应商信息。
    """
    try:
        return await asyncio.to_thread(
            items.query_items,
            runtime.context.erp,
            {"query": query, "offset": offset, "limit": limit},
        )
    except AgentError as exc:
        if exc.status != 400:
            raise
        return {"error": str(exc)}
