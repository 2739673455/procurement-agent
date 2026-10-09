"""只读物料工具，认证客户端绑定到本轮工具闭包，不进入模型或持久状态。"""

import json
from typing import Annotated

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolChunk
from loguru import logger
from pydantic import ConfigDict, Field, validate_call

from app.clients.erpnext import items
from app.errors.agent import AgentError
from app.runtime.context import RunContext


def create_items_tool(context: RunContext) -> FunctionTool:
    """从认证运行上下文取得 ERPNext 客户端，创建只读物料查询工具。"""
    erp = context.clients["erpnext"]

    @validate_call(config=ConfigDict(strict=True))
    async def query_items(
        fields: list[str] | None = None,
        filters: items.Filters | None = None,
        or_filters: items.Filters | None = None,
        order_by: str | None = None,
        limit_start: Annotated[int, Field(ge=0)] = 0,
        limit_page_length: Annotated[int, Field(ge=1)] = 20,
    ) -> ToolChunk:
        r"""调用 GET /api/resource/Item，只读查询当前用户有权访问的物料。

        使用 ERPNext 原生参数和返回值，不修改操作符、查询值或通配符。
        filters 中的条件按 AND 组合，or_filters 中的条件按 OR 组合；
        两组同时提供时组合为 filters AND (or_filters)。
        条件可写为 [字段, 操作符, 值] 列表，或 {字段: [操作符, 值]} 字典。
        不传过滤条件时分页列出物料。

        精确匹配用 ["item_code", "=", "ABC_1"]，此时 %、_ 均为普通字符。
        LIKE 用 ["item_code", "like", "%ABC%"] 查询包含 ABC 的编码。
        LIKE 中 % 匹配任意长度字符，_ 匹配一个字符；工具不会自动添加 %。
        匹配字面上的 %、_ 时，在其前面加反斜杠。
        JSON 示例：["item_code", "like", "%ABC\\_1%"]，
        匹配包含 ABC_1 的编码，其中 JSON 的双反斜杠表示一个反斜杠。

        fields 指定返回字段；不传时接口默认只返回 name。
        常用字段：name、item_code、item_name、item_group、stock_uom、disabled。
        order_by 示例："name asc"。limit_start 为起始位置，
        limit_page_length 为单页数量，默认分别为 0 和 20。
        返回 ERPNext 原生 JSON，物料列表位于 data，不包含总数或 has_more。
        单页不足 limit_page_length 时已到末页；满页需继续查询才能确认。
        """
        try:
            result = await items.query_items(
                erp,
                fields=fields,
                filters=filters,
                or_filters=or_filters,
                order_by=order_by,
                limit_start=limit_start,
                limit_page_length=limit_page_length,
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
        is_read_only=True,  # 供框架权限引擎识别只读调用。
        permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="通过当前 ERPNext 用户权限执行只读查询。",
        ),
    )
