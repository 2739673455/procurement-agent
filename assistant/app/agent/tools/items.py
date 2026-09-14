"""物料查询工具的接口定义与执行边界。"""

import asyncio

from app.errors import AgentError
from app.integrations.erpnext import ERPNext

TOOL = {
    "type": "function",
    "function": {
        "name": "query_items",
        "description": "只读查询当前用户可访问的 ERPNext Item。按编码或名称搜索，可分页；不提供库存、价格或供应商信息。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "物料编码或名称关键词，空字符串列出物料",
                },
                "offset": {"type": "integer", "minimum": 0, "maximum": 1000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


async def query_items(erp: ERPNext, args):
    try:
        return await asyncio.to_thread(erp.query_items, args)
    except AgentError as exc:
        if exc.status != 400:
            raise
        return {"error": str(exc)}
