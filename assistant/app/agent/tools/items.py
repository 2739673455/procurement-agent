"""只读物料工具，认证客户端绑定到本轮工具闭包，不进入模型或持久状态。"""

import asyncio
import json
from typing import Annotated

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolChunk
from loguru import logger
from pydantic import Field

from app.clients.erpnext import items
from app.clients.erpnext.client import ERPNext
from app.errors.agent import AgentError


def create_items_tool(erp: ERPNext) -> FunctionTool:
    """创建只读物料查询工具，erp 为已绑定用户登录身份的客户端。"""

    async def query_items(
        query: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1)] = 20,
    ) -> ToolChunk:
        """只读查询当前用户可访问的 ERPNext Item。

        query 为物料编码或名称关键词，空字符串列出物料。
        offset 和 limit 控制分页；不提供库存、价格或供应商信息。
        """
        try:
            result = await asyncio.to_thread(
                items.query_items,
                erp,
                {"query": query, "offset": offset, "limit": limit},
            )
        except AgentError as exc:
            return ToolChunk(
                content=[
                    TextBlock(text=json.dumps({"error": str(exc)}, ensure_ascii=False))
                ],
                state=ToolResultState.ERROR,
            )
        except Exception as exc:  # noqa: BLE001 -- 原始异常不能成为模型或前端可见的工具结果
            logger.opt(exception=exc).error("物料工具执行失败")
            return ToolChunk(
                content=[TextBlock(text='{"error":"物料查询失败，请稍后重试。"}')],
                state=ToolResultState.ERROR,
            )
        return ToolChunk(
            content=[TextBlock(text=json.dumps(result, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )

    return FunctionTool(
        query_items,
        is_read_only=True,
        permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="通过当前 ERPNext 用户权限执行只读查询。",
        ),
    )
