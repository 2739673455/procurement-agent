"""Same-origin authenticated JSON/SSE gateway; ERPNext remains the identity authority."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import frappe
from werkzeug.wrappers import Response


@frappe.whitelist(methods=["POST"])
def conversations(action, conversation_id=None, message="", title=""):
    if frappe.session.user == "Guest":
        frappe.throw("请先登录。", frappe.PermissionError)
    payload = {
        "action": action,
        "conversation_id": conversation_id,
        "message": message,
        "title": title,
        "sid": frappe.session.sid,
    }
    data = json.dumps(payload).encode()
    if len(data) > 20000:
        return {"error": "请求过大。"}
    base = os.getenv(
        "PROCUREMENT_AGENT_URL", "http://host.docker.internal:8100"
    ).rstrip("/")
    request = Request(
        base + "/conversations", data=data, headers={"Content-Type": "application/json"}
    )
    try:
        upstream = urlopen(request, timeout=35)
    except HTTPError as exc:
        try:
            with exc:
                return {"error": json.load(exc).get("error", "助手请求失败。")}
        except (ValueError, AttributeError):
            return {"error": "助手请求失败。"}
    except (URLError, TimeoutError):
        return {"error": "助手服务暂时不可用。"}
    if action not in ("send", "subscribe"):
        with upstream:
            return json.load(upstream)

    def stream():
        try:
            with upstream:
                yield from upstream
        except (OSError, ValueError):
            yield (
                "data: "
                + json.dumps({"type": "error", "error": "连接中断，请重新打开对话。"})
                + "\n\n"
            ).encode()

    response = Response(
        stream(),
        content_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    response.call_on_close(upstream.close)
    return response
