"""ERPNext 异步连接池的用户隔离、原生查询和错误边界。"""

import asyncio
import json

import httpx
import pytest

from app.clients.erpnext.client import ERPNext
from app.clients.erpnext.items import query_items
from app.errors.agent import AgentError


def test_shared_erpnext_pool_keeps_user_cookies_separate():
    """并发认证及后续查询保留各自身份，不使用连接池收到的其他 Cookie。"""

    async def scenario():
        seen = []

        async def handle(request):
            sid = request.headers["Cookie"]
            seen.append((request.url.path, sid))
            assert request.headers["Host"] == "erp.test"
            await asyncio.sleep(0)
            if request.url.path == "/api/method/frappe.auth.get_logged_user":
                return httpx.Response(
                    200,
                    json={"message": "alice" if sid == "sid=A" else "bob"},
                    headers={"Set-Cookie": "sid=server-cookie; Path=/"},
                )
            assert request.url.path == "/api/resource/Item"
            assert json.loads(request.url.params["filters"]) == [
                ["item_code", "like", r"%ABC\_1%"]
            ]
            return httpx.Response(200, json={"data": [{"name": "ABC_1"}]})

        async with httpx.AsyncClient(
            base_url="http://erp.test",
            headers={"Host": "erp.test"},
            transport=httpx.MockTransport(handle),
        ) as pool:
            alice, bob = ERPNext("A", pool), ERPNext("B", pool)
            assert await asyncio.gather(alice.authenticate(), bob.authenticate()) == [
                "alice",
                "bob",
            ]
            assert await query_items(
                alice, filters=[["item_code", "like", r"%ABC\_1%"]]
            ) == {"data": [{"name": "ABC_1"}]}
            assert not pool.is_closed
        assert pool.is_closed
        assert seen == [
            ("/api/method/frappe.auth.get_logged_user", "sid=A"),
            ("/api/method/frappe.auth.get_logged_user", "sid=B"),
            ("/api/resource/Item", "sid=A"),
        ]

    asyncio.run(scenario())


def test_erpnext_reports_authentication_and_upstream_errors():
    """登录、权限、连接和响应错误转换为业务错误，保留适当的 HTTP 状态。"""

    async def scenario():
        def handle(request):
            case = request.headers["Cookie"].removeprefix("sid=")
            if case == "timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            if case == "invalid-json":
                return httpx.Response(200, text="invalid JSON")
            if case == "guest":
                return httpx.Response(200, json={"message": "Guest"})
            return httpx.Response(int(case), json={"error": "upstream error"})

        async with httpx.AsyncClient(
            base_url="http://erp.test", transport=httpx.MockTransport(handle)
        ) as pool:
            for case, status in (
                ("401", 403),
                ("403", 403),
                ("500", 502),
                ("timeout", 502),
                ("invalid-json", 502),
                ("guest", 401),
            ):
                with pytest.raises(AgentError) as failure:
                    await ERPNext(case, pool).authenticate()
                assert failure.value.status == status

    asyncio.run(scenario())
