"""采购工具的 HTTP 合约、角色范围、参数和权限边界。"""

import asyncio
import json
from typing import Any

import httpx
import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from app.clients.erpnext.client import ERPNext
from app.runtime.context import RunContext
from app.tools import procurement


def payload(chunk: ToolChunk) -> dict[str, Any]:
    block = chunk.content[0]
    assert isinstance(block, TextBlock)
    return json.loads(block.text)


def test_procurement_read_contracts() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.method == "GET"
            assert request.headers["Cookie"] == "sid=reader"
            return httpx.Response(200, json={"data": [{"name": "物料/A"}]})

        async with httpx.AsyncClient(
            base_url="https://erp.test", transport=httpx.MockTransport(handle)
        ) as pool:
            context = RunContext(
                "reader", "session", {"erpnext": ERPNext("reader", pool)}
            )
            tool = procurement.create_list_documents_tool(context)
            result = await tool(
                doctype="Purchase Order",
                filters={"supplier": "供应商"},
                fields=["name", "modified"],
                limit_start=20,
            )
            assert isinstance(result, ToolChunk)
            assert result.state == ToolResultState.SUCCESS
            assert payload(result)["data"][0]["name"] == "物料/A"
            assert json.loads(requests[-1].url.params["filters"]) == {
                "supplier": "供应商"
            }
            assert requests[-1].url.params["limit_start"] == "20"
            count = len(requests)
            for params in (
                {"doctype": "User"},
                {"doctype": "Item", "limit_page_length": 201},
                {"doctype": "Item", "limit_start": True},
            ):
                invalid = await tool(**params)
                assert isinstance(invalid, ToolChunk)
                assert invalid.state == ToolResultState.ERROR
            assert len(requests) == count
            await procurement.create_get_document_tool(context)(
                doctype="Item", name="物料/A?x=1"
            )
            assert b"%2FA%3Fx%3D1" in requests[-1].url.raw_path
            await procurement.create_get_schema_tool(context)(doctype="Purchase Order")
            assert requests[-1].url.path == "/api/v2/doctype/Purchase Order/meta"
            await procurement.create_get_schema_tool(context)(
                doctype="Purchase Order Item"
            )
            assert requests[-1].url.path == "/api/v2/doctype/Purchase Order Item/meta"
            await procurement.create_run_report_tool(context)(
                report_name="Purchase Order Analysis", filters={"company": "公司"}
            )
            assert requests[-1].url.path == "/api/method/frappe.desk.query_report.run"
            await procurement.create_stock_tool(context)(
                item_code="A", warehouse="Main"
            )
            assert requests[-1].url.path == "/api/resource/Bin"
            assert json.loads(requests[-1].url.params["filters"]) == {
                "item_code": "A",
                "warehouse": "Main",
            }

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [403, 500])
def test_query_errors_do_not_leak_upstream(status: int) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://erp.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, text="secret-session-and-stack")
            ),
        ) as pool:
            tool = procurement.create_get_document_tool(
                RunContext("user", "session", {"erpnext": ERPNext("secret", pool)})
            )
            result = await tool(doctype="Item", name="A")
            assert isinstance(result, ToolChunk)
            assert result.state == ToolResultState.ERROR
            assert "secret" not in str(payload(result))

    asyncio.run(scenario())
