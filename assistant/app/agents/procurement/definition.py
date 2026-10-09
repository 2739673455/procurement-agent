"""将采购业务工具注册到通用 Agent 能力目录。"""

from app.agents.procurement.tools.items import create_items_tool


def items(context):
    """使用本次任务获准的 ERPNext 客户端创建物料工具。"""
    return [create_items_tool(context.clients["erpnext"])]


TOOL_FACTORIES = {"items": items}
