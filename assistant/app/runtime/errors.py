"""运行控制异常，由调用方决定如何呈现。"""

from typing import Literal

type RunErrorCode = Literal[
    "invalid_input", "not_found", "context_expired", "conflict", "unavailable"
]


class RunError(Exception):
    """携带失败原因和说明，不绑定 HTTP 响应。"""

    def __init__(self, message: str, code: RunErrorCode) -> None:
        """保存调用方可识别的错误码。"""
        super().__init__(message)
        self.code = code
