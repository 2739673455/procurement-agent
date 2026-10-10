"""业务工具参数校验与 ERPNext 调用边界。"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk
from pydantic import ValidationError

from app.clients.erpnext.items import Filters
from app.runtime.context import RunContext
from app.tools.items import create_items_tool


def test_item_tool_rejects_invalid_inputs_before_request() -> None:
    """拒绝错误类型和分页范围，无过滤条件时分页列出物料。"""
    client = Mock()
    client.get = AsyncMock(return_value={"data": []})
    tool = create_items_tool(RunContext("user", "session", {"erpnext": client}))

    async def scenario() -> None:
        for parameters in (
            {"fields": "item_code"},
            {"filters": "bolt"},
            {"limit_start": -1},
            {"limit_start": True},
            {"limit_page_length": 0},
            {"limit_page_length": "20"},
            {"limit_page_length": 1.5},
        ):
            with pytest.raises(ValidationError):
                await tool(**parameters)
        client.get.assert_not_awaited()
        result = await tool()
        assert isinstance(result, ToolChunk)
        assert result.state == ToolResultState.SUCCESS
        client.get.assert_awaited_once_with(
            "/api/resource/Item", {"limit_start": 0, "limit_page_length": 20}
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "filters",
    (
        {"item_code": ["=", "ABC_1"]},
        [["item_name", "like", r"  %ABC\_1%  "]],
    ),
)
def test_item_tool_preserves_native_filters_and_response(
    filters: Filters | None,
) -> None:
    """过滤值、通配符转义、分页数量和响应字段均按原生接口保留。"""
    client = Mock()
    payload = {"data": [{"name": "ABC_1", "item_name": "物料", "description": "说明"}]}
    client.get = AsyncMock(return_value=payload)
    tool = create_items_tool(RunContext("user", "session", {"erpnext": client}))
    fields = ["name", "item_name", "description"]
    or_filters = [["item_code", "like", r"%50\%%"], ["item_name", "=", "ABC_1"]]
    result = asyncio.run(
        tool(
            fields=fields,
            filters=filters,
            or_filters=or_filters,
            order_by="item_code desc",
            limit_start=5,
            limit_page_length=2,
        )
    )
    assert isinstance(result, ToolChunk)
    assert result.state == ToolResultState.SUCCESS
    content = result.content[0]
    assert isinstance(content, TextBlock)
    assert json.loads(content.text) == payload
    path, parameters = client.get.call_args.args
    assert path == "/api/resource/Item"
    assert json.loads(parameters["fields"]) == fields
    assert json.loads(parameters["filters"]) == filters
    assert json.loads(parameters["or_filters"]) == or_filters
    assert parameters["order_by"] == "item_code desc"
    assert parameters["limit_start"] == 5
    assert parameters["limit_page_length"] == 2
