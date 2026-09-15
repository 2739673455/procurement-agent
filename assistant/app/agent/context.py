"""单轮执行上下文，不写入 Checkpoint。"""

from dataclasses import dataclass

from app.clients.erpnext.client import ERPNext


@dataclass
class TurnContext:
    erp: ERPNext
