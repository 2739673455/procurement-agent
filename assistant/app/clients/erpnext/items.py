"""ERPNext 物料查询接口，共用已认证客户端的连接配置。"""

import json

from app.clients.erpnext.client import ERPNext
from app.errors.agent import AgentError

FIELDS = ["name", "item_code", "item_name", "item_group", "stock_uom", "disabled"]


def query_items(client: ERPNext, args):
    if not isinstance(args, dict) or set(args) - {"query", "offset", "limit"}:
        raise AgentError("Item 查询参数无效。")
    query, offset, limit = (
        args.get("query"),
        args.get("offset", 0),
        args.get("limit", 10),
    )
    if not isinstance(query, str):
        raise AgentError("物料关键词须为文本。")
    if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
        raise AgentError("分页参数无效。")
    params = {
        "fields": json.dumps(FIELDS),
        "limit_start": offset,
        "limit_page_length": limit + 1,
        "order_by": "name asc",
    }
    if query.strip():
        if any(c in query for c in ("%", "_", "\\")):
            # 编码包含 SQL LIKE 通配符时，改用精确查询。
            op, value = "=", query.strip()
        else:
            op, value = "like", "%" + query.strip() + "%"
        params["or_filters"] = json.dumps(
            [["item_code", op, value], ["item_name", op, value]]
        )
    data = client.get("/api/resource/Item", params).get("data")
    if not isinstance(data, list):
        raise AgentError("ERPNext 返回的 Item 数据格式无效。", 502)
    rows = [{k: row[k] for k in FIELDS if k in row} for row in data[:limit]]
    return {
        "items": rows,
        "offset": offset,
        "limit": limit,
        "has_more": len(data) > limit,
    }
