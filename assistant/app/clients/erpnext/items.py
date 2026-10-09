"""ERPNext 物料查询接口，共用已认证客户端的连接配置。"""

import json
from typing import Any

from app.clients.erpnext.client import ERPNext

# 过滤条件沿用 Frappe 的列表或字典形式，操作符和查询值由接口解释。
type Filters = list[list[Any]] | dict[str, Any]


async def query_items(
    client: ERPNext,
    *,
    fields: list[str] | None = None,
    filters: Filters | None = None,
    or_filters: Filters | None = None,
    order_by: str | None = None,
    limit_start: int = 0,
    limit_page_length: int = 20,
):
    """发送 Item 列表接口参数，返回 ERPNext 的原生 JSON 响应。"""
    params: dict[str, int | str] = {
        "limit_start": limit_start,
        "limit_page_length": limit_page_length,
    }
    for name, value in (
        ("fields", fields),
        ("filters", filters),
        ("or_filters", or_filters),
    ):
        if value is not None:
            params[name] = json.dumps(value, ensure_ascii=False)
    if order_by is not None:
        params["order_by"] = order_by
    return await client.get("/api/resource/Item", params)
