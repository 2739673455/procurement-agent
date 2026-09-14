"""Authenticated chat entry point; business reads use ERPNext's native API."""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import frappe


@frappe.whitelist(methods=["POST"])
def chat(message, history=None):
    if frappe.session.user == "Guest":
        frappe.throw("请先登录。", frappe.PermissionError)
    if not isinstance(message, str) or not message.strip() or len(message) > 4000:
        frappe.throw("请输入 1–4000 字的问题。")
    history = frappe.parse_json(history) if isinstance(history, str) else history
    payload = {"message": message, "history": history or [], "sid": frappe.session.sid}
    data = json.dumps(payload).encode()
    if len(data) > 200_000:
        frappe.throw("对话过长，请新建对话。")
    agent_url = os.environ.get("PROCUREMENT_AGENT_URL", "http://host.docker.internal:8100").rstrip("/")
    request = Request(agent_url + "/chat", data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=95) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            error = json.load(exc).get("error", "Agent 请求失败。")
        except (ValueError, AttributeError):
            error = "Agent 请求失败。"
        # Return expected operational failures as data, not HTML error dialogs.
        return {"error": error}
    except (URLError, TimeoutError):
        return {"error": "Agent 服务暂时不可用，请稍后重试。"}
