"""遵循当前用户权限的 ERPNext 只读客户端。"""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import app_config
from app.errors.agent import AgentError


def request_json(url, *, headers=None, payload=None, timeout: float = 15):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=data, headers=headers or {})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


class ERPNext:
    def __init__(self, sid: str):
        settings = app_config.cfg.erpnext
        self.base = settings.base_url.rstrip("/")
        self.timeout = settings.timeout_seconds
        self.headers = {
            "Cookie": "sid=" + sid,
            "Host": settings.site,
        }

    def get(self, path, params=None):
        url = self.base + path + ("?" + urlencode(params) if params else "")
        try:
            return request_json(url, headers=self.headers, timeout=self.timeout)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise AgentError(
                    "当前用户无查询权限，或登录会话已失效。", 403
                ) from None
            raise AgentError("ERPNext 查询失败，请稍后重试。", 502) from None
        except (URLError, TimeoutError, ValueError):
            raise AgentError("无法连接 ERPNext，请稍后重试。", 502) from None

    def authenticate(self):
        user = self.get("/api/method/frappe.auth.get_logged_user").get("message")
        if not user or user == "Guest":
            raise AgentError("请先登录 ERPNext。", 401)
        return user
