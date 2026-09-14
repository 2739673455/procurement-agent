"""遵循当前用户权限的 ERPNext 只读客户端。"""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import app_config
from app.errors import AgentError

FIELDS = ["name", "item_code", "item_name", "item_group", "stock_uom", "disabled"]


def request_json(url, *, headers=None, payload=None, timeout: float = 15):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=data, headers=headers or {})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


class ERPNext:
    def __init__(self, sid):
        if not isinstance(sid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,256}", sid):
            raise AgentError("登录会话无效，请重新登录。", 401)
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

    def query_items(self, args):
        if not isinstance(args, dict) or set(args) - {"query", "offset", "limit"}:
            raise AgentError("Item 查询参数无效。")
        query, offset, limit = (
            args.get("query"),
            args.get("offset", 0),
            args.get("limit", 10),
        )
        if not isinstance(query, str) or len(query) > 100:
            raise AgentError("物料关键词须为不超过 100 字的文本。")
        if (
            type(offset) is not int
            or not 0 <= offset <= 1000
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise AgentError("分页参数无效。")
        params = {
            "fields": json.dumps(FIELDS),
            "limit_start": offset,
            "limit_page_length": limit + 1,
            "order_by": "name asc",
        }
        if query.strip():
            if any(c in query for c in ("%", "_", "\\")):
                # 编码包含 SQL LIKE 通配符时，改用精确查询。
                op, value = "=", query.strip()
            else:
                op, value = "like", "%" + query.strip() + "%"
            params["or_filters"] = json.dumps(
                [["item_code", op, value], ["item_name", op, value]]
            )
        data = self.get("/api/resource/Item", params).get("data")
        if not isinstance(data, list):
            raise AgentError("ERPNext 返回的 Item 数据格式无效。", 502)
        rows = [{k: row[k] for k in FIELDS if k in row} for row in data[:limit]]
        return {
            "items": rows,
            "offset": offset,
            "limit": limit,
            "has_more": len(data) > limit,
        }
