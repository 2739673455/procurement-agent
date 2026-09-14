"""可以直接向用户展示的预期应用错误。"""


class AgentError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
