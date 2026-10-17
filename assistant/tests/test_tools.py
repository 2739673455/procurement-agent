"""业务工具参数校验与 ERPNext 调用边界。"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from app.clients.erpnext.procurement import Filters
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
            {"limit_page_length": 201},
            {"limit_page_length": True},
            {"doctype": "Supplier"},
            {"limit_page_length": "20"},
            {"limit_page_length": 1.5},
        ):
            result = await tool(**parameters)
            assert isinstance(result, ToolChunk)
            assert result.state == ToolResultState.ERROR
        client.get.assert_not_awaited()
        result = await tool()
        assert isinstance(result, ToolChunk)
        assert result.state == ToolResultState.SUCCESS
        client.get.assert_awaited_once_with(
            "/api/resource/Item", {"limit_start": 0, "limit_page_length": 20}
        )
        for size in (1, 200):
            result = await tool(limit_page_length=size)
            assert isinstance(result, ToolChunk)
            assert result.state == ToolResultState.SUCCESS
            assert client.get.call_args.args[1]["limit_page_length"] == size

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


@pytest.mark.parametrize("business_error", [True, False])
def test_item_tool_uses_shared_error_handling_and_read_only_permission(
    business_error: bool,
) -> None:
    from agentscope.permission import PermissionBehavior

    from app.errors.agent import AgentError

    client = Mock()
    client.get = AsyncMock(
        side_effect=AgentError("当前用户无查询权限。", 403)
        if business_error
        else RuntimeError("secret-credential-and-stack")
    )
    tool = create_items_tool(RunContext("user", "session", {"erpnext": client}))

    async def scenario() -> None:
        assert tool.name == "query_items" and tool.is_read_only
        assert (await tool.check_permissions({})).behavior == PermissionBehavior.ALLOW
        result = await tool()
        assert isinstance(result, ToolChunk)
        assert result.state == ToolResultState.ERROR
        block = result.content[0]
        assert isinstance(block, TextBlock)
        error = json.loads(block.text)["error"]
        assert "secret" not in error
        assert error == (
            "当前用户无查询权限。"
            if business_error
            else "ERPNext 工具执行失败，请检查参数或重新查询单据状态。"
        )
        client.get.assert_awaited_once()

    asyncio.run(scenario())
