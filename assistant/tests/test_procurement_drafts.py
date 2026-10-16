"""采购草稿、映射和认证写入边界。"""

import asyncio
import json

import httpx
import pytest
from agentscope.message import ToolResultState
from agentscope.tool import ToolChunk

from app.clients.erpnext.client import ERPNext
from app.clients.erpnext.drafts import CONVERSIONS, prepare_document, save_draft
from app.errors.agent import AgentError
from app.runtime.context import RunContext
from app.tools.drafts import create_save_draft_tool


def test_draft_writes_preserve_version_identity_and_links() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []
        original = {
            "doctype": "Purchase Order",
            "name": "PO-1",
            "docstatus": 0,
            "modified": "v1",
            "supplier": "S",
            "items": [{"name": "row-1", "material_request_item": "mr-row", "qty": 1}],
        }

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.headers["Cookie"] == "sid=alice"
            if request.method == "GET":
                return httpx.Response(200, json={"data": original})
            assert request.headers["X-Frappe-CSRF-Token"] == "csrf-alice"
            return httpx.Response(
                200, json={"message": json.loads(request.content)["doc"]}
            )

        async with httpx.AsyncClient(
            base_url="https://erp.test", transport=httpx.MockTransport(handle)
        ) as pool:
            erp = ERPNext("alice", pool, "csrf-alice")
            result = await save_draft(
                erp, "Purchase Order", {"supplier": "new"}, "PO-1", "v1"
            )
            assert result["message"]["modified"] == "v1"
            assert result["message"]["items"] == original["items"]
            assert requests[-1].url.path.endswith("frappe.client.save")
            for fields in (
                {"flags": {"ignore_permissions": True}},
                {"docstatus": 1},
                {"items": [{"flags": {}}]},
                {"workflow_state": "Approved"},
            ):
                count = len(requests)
                with pytest.raises(AgentError):
                    await save_draft(erp, "Purchase Order", fields)
                assert len(requests) == count
            with pytest.raises(AgentError, match="已被修改"):
                await save_draft(
                    erp, "Purchase Order", {"supplier": "x"}, "PO-1", "old"
                )
            assert requests[-1].method == "GET"
            original["docstatus"] = 1
            with pytest.raises(AgentError, match="草稿"):
                await save_draft(erp, "Purchase Order", {"supplier": "x"}, "PO-1", "v1")
            await save_draft(
                erp,
                "Supplier Quotation",
                {"supplier": "S", "items": [{"item_code": "A", "qty": 1}]},
            )
            assert requests[-1].url.path.endswith("frappe.client.insert")
            assert json.loads(requests[-1].content)["doc"]["docstatus"] == 0

    asyncio.run(scenario())


def test_all_mappings_only_call_fixed_native_methods() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "docstatus": 1,
                            "modified": "v1",
                            "material_request_type": "Purchase",
                            "suppliers": [{"supplier": "S"}],
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "message": {
                        "doctype": "Purchase Order",
                        "items": [{"material_request_item": "source-row"}],
                    }
                },
            )

        async with httpx.AsyncClient(
            base_url="https://erp.test", transport=httpx.MockTransport(handle)
        ) as pool:
            erp = ERPNext("alice", pool, "csrf")
            for conversion, (_, method) in CONVERSIONS.items():
                result = await prepare_document(
                    erp,
                    conversion,
                    "source",
                    "S" if conversion == "rfq_to_quotation" else None,
                )
                assert result["saved"] is False
                assert (
                    result["document"]["items"][0]["material_request_item"]
                    == "source-row"
                )
                assert requests[-1].url.path == "/api/method/" + method
            with pytest.raises(AgentError, match="供应商"):
                await prepare_document(erp, "rfq_to_quotation", "source", "unknown")
            assert requests[-1].method == "GET"

    asyncio.run(scenario())


def test_write_csrf_and_unknown_outcome_are_not_retried() -> None:
    async def scenario() -> None:
        seen = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            raise httpx.ReadTimeout("secret", request=request)

        async with httpx.AsyncClient(
            base_url="https://erp.test", transport=httpx.MockTransport(handle)
        ) as pool:
            with pytest.raises(AgentError, match="CSRF"):
                await ERPNext("alice", pool).post("/api/method/test", {})
            assert not seen
            tool = create_save_draft_tool(
                RunContext("u", "s", {"erpnext": ERPNext("alice", pool, "csrf")})
            )
            result = await tool(doctype="Purchase Order", fields={"supplier": "S"})
            assert isinstance(result, ToolChunk)
            assert result.state == ToolResultState.ERROR
            assert "结果未知" in str(result.content)
            assert "secret" not in str(result.content)
            assert len(seen) == 1

    asyncio.run(scenario())
