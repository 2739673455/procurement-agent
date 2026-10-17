"""只读物料工具，认证客户端绑定到本轮工具闭包，不进入模型或持久状态。"""

from typing import Any

from agentscope.tool import FunctionTool

from app.clients.erpnext import procurement as api
from app.clients.erpnext.client import ERPNext
from app.runtime.context import RunContext
from app.tools.common import erp_tool


def create_items_tool(context: RunContext) -> FunctionTool:
    """从认证运行上下文取得 ERPNext 客户端，创建只读物料查询工具。"""
    erp: ERPNext = context.clients["erpnext"]

    async def query_items(
        fields: list[str] | None = None,
        filters: api.Filters | None = None,
        or_filters: api.Filters | None = None,
        order_by: str | None = None,
        limit_start: api.PageStart = 0,
        limit_page_length: api.PageSize = 20,
    ) -> dict[str, Any]:
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
        limit_page_length 为单页数量（1–200），默认分别为 0 和 20。
        返回 ERPNext 原生 JSON，物料列表位于 data，不包含总数或 has_more。
        单页不足 limit_page_length 时已到末页；满页需继续查询才能确认。
        """
        return await api.list_documents(
            erp,
            "Item",
            fields=fields,
            filters=filters,
            or_filters=or_filters,
            order_by=order_by,
            limit_start=limit_start,
            limit_page_length=limit_page_length,
        )

    return erp_tool(query_items)
