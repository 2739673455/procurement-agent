"""业务工具共用严格参数验证、结果封装和错误脱敏。"""

import json
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolChunk
from loguru import logger
from pydantic import ConfigDict, validate_call

from app.errors.agent import AgentError


def erp_tool(
    function: Callable[..., Awaitable[dict[str, Any]]], *, read_only: bool = True
) -> FunctionTool:
    """参数签名保留给框架；所有写入都必须先经过原生工具确认。"""
    validated = validate_call(config=ConfigDict(strict=True))(function)

    @wraps(function)
    async def call(*args: Any, **kwargs: Any) -> ToolChunk:
        try:
            result = await validated(*args, **kwargs)
        except AgentError as exc:
            result = {"error": str(exc)}
            state = ToolResultState.ERROR
        except Exception:  # noqa: BLE001 -- 上游响应、凭据和堆栈不得进入模型
            logger.error("ERPNext 工具 {} 执行失败", function.__name__)
            result = {"error": "ERPNext 工具执行失败，请检查参数或重新查询单据状态。"}
            state = ToolResultState.ERROR
        else:
            state = ToolResultState.SUCCESS
        return ToolChunk(
            content=[TextBlock(text=json.dumps(result, ensure_ascii=False))],
            state=state,
        )

    return FunctionTool(
        call,
        is_read_only=read_only,
        permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW if read_only else PermissionBehavior.ASK,
            message="按当前 ERPNext 用户权限查询。"
            if read_only
            else "请核对目标单据、参数及业务影响后确认执行；ERPNext 仍会校验权限和工作流。",
        ),
    )
