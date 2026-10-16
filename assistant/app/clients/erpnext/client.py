"""遵循当前用户权限的 ERPNext 客户端。"""

from typing import Any

import httpx

from app.errors.agent import AgentError


class ERPNext:
    """复用应用 HTTP 连接池，通过独立用户 Cookie 访问 ERPNext。"""

    def __init__(
        self, sid: str, client: httpx.AsyncClient, csrf_token: str | None = None
    ) -> None:
        """绑定用户凭据；连接池的创建和释放由应用生命周期负责。"""
        self._client = client
        self._headers = {"Cookie": "sid=" + sid}
        if csrf_token:
            self._headers["X-Frappe-CSRF-Token"] = csrf_token

    async def get(
        self, path: str, params: dict[str, str | int] | None = None
    ) -> dict[str, Any]:
        """携带用户身份发起 GET 请求，将上游异常转换为可展示的业务错误。"""
        try:
            response = await self._client.get(
                path, params=params, headers=self._headers
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise AgentError(
                    "当前用户无查询权限，或登录会话已失效。", 403
                ) from None
            raise AgentError("ERPNext 查询失败，请稍后重试。", 502) from None
        except (httpx.RequestError, ValueError, TypeError):
            raise AgentError("无法连接 ERPNext，请稍后重试。", 502) from None

    async def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """携带当前会话 CSRF 发起写请求；绝不自动重试不确定的执行结果。"""
        if "X-Frappe-CSRF-Token" not in self._headers:
            raise AgentError("缺少当前 ERPNext 会话的 CSRF 凭据，请刷新页面重试。", 403)
        try:
            response = await self._client.post(path, json=data, headers=self._headers)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise TypeError("响应不是 JSON 对象")
            return result
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise AgentError(
                    "ERPNext 权限、登录会话或 CSRF 校验失败。", 403
                ) from None
            if status in (409, 417):
                raise AgentError(
                    "ERPNext 拒绝操作：请重新读取单据，核对版本、必填项和业务状态。",
                    409,
                ) from None
            raise AgentError(
                "ERPNext 操作未获成功确认，请先查询单据状态，不要直接重试。", 502
            ) from None
        except (httpx.RequestError, ValueError, TypeError):
            raise AgentError(
                "ERPNext 操作结果未知，请先查询单据状态，不要直接重试。", 502
            ) from None

    async def authenticate(self) -> str:
        """查询登录用户标识，拒绝匿名或失效的登录会话。"""
        user = (await self.get("/api/method/frappe.auth.get_logged_user")).get(
            "message"
        )
        if not user or user == "Guest":
            raise AgentError("请先登录 ERPNext。", 401)
        return user
