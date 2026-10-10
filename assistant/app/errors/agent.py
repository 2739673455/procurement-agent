"""可以向用户展示的采购助手业务异常。"""

from app.errors.base import ProblemError


class AgentError(ProblemError):
    """采购助手可向用户展示的业务错误，包含提示文本和 HTTP 状态。"""

    type = "agent-error"

    def __init__(self, message: str, status: int = 400) -> None:
        """将业务提示作为错误标题，默认使用 HTTP 400 状态。"""
        super().__init__(title=message, status=status)
