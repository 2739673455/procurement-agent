"""可以向用户展示的采购助手业务异常。"""

from app.errors.base import ProblemError


class AgentError(ProblemError):
    type = "agent-error"

    def __init__(self, message: str, status: int = 400):
        super().__init__(title=message, status=status)
