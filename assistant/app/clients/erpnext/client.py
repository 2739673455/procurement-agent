"""遵循当前用户权限的 ERPNext 只读客户端。"""

from typing import Any

import httpx

from app.errors.agent import AgentError


class ERPNext:
    """复用应用 HTTP 连接池，通过独立用户 Cookie 访问 ERPNext。"""

    def __init__(self, sid: str, client: httpx.AsyncClient) -> None:
        """绑定用户凭据；连接池的创建和释放由应用生命周期负责。"""
        self.client = client
        self.headers = {"Cookie": "sid=" + sid}

    async def get(
        self, path: str, params: dict[str, str | int] | None = None
    ) -> dict[str, Any]:
        """携带用户身份发起 GET 请求，将上游异常转换为可展示的业务错误。"""
        try:
            response = await self.client.get(path, params=params, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise AgentError(
                    "当前用户无查询权限，或登录会话已失效。", 403
                ) from None
            raise AgentError("ERPNext 查询失败，请稍后重试。", 502) from None
        except (httpx.RequestError, ValueError):
            raise AgentError("无法连接 ERPNext，请稍后重试。", 502) from None

    async def authenticate(self) -> str:
        """查询登录用户标识，拒绝匿名或失效的登录会话。"""
        user = (await self.get("/api/method/frappe.auth.get_logged_user")).get(
            "message"
        )
        if not user or user == "Guest":
            raise AgentError("请先登录 ERPNext。", 401)
        return user
