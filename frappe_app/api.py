"""将 ERPNext 页面的同源请求转发给 Assistant，身份由 ERPNext 登录会话提供。"""

import json
import os
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import frappe
from procurement_assistant.context import attachment_payloads, page_snapshot
from werkzeug.wrappers import Response


def _stream_response(upstream):
    """逐行转发上游 SSE 数据和心跳，不等待整个 Agent 任务完成。"""
    try:
        with upstream:
            yield from upstream
    except (OSError, ValueError, HTTPException):
        # 流已经开始，无法再改 HTTP 响应，改用 SSE 错误事件通知页面。
        yield (
            "data: "
            + json.dumps({"type": "error", "error": "连接中断，请重新打开对话。"})
            + "\n\n"
        ).encode()


# 将函数开放为 Frappe POST 接口，默认不允许访客调用。
@frappe.whitelist(methods=["POST"])
def conversations(
    action,
    conversation_id=None,
    message="",
    title="",
    page_context=None,
    attachments=None,
):
    """代理会话操作：普通操作返回 JSON，发送消息和订阅返回 SSE 流。"""
    # 转发当前登录会话，供 Assistant 验证身份。
    payload = {
        "action": action,
        "conversation_id": conversation_id,
        "message": message,
        "title": title,
        "sid": frappe.session.sid,
    }
    if action == "send":
        payload["page_context"] = page_snapshot(page_context)
        payload["attachments"] = attachment_payloads(attachments)
    data = json.dumps(payload).encode()
    # 从环境变量读取 Assistant 服务地址。
    base = os.environ["PROCUREMENT_AGENT_URL"].rstrip("/")
    # 为本次代理请求生成关联标识，同时传给 Assistant 和浏览器，便于查日志。
    trace_id = str(uuid4())
    frappe.local.response_headers["X-Trace-ID"] = trace_id
    request = Request(
        base + "/conversations",
        data=data,
        headers={"Content-Type": "application/json", "X-Trace-ID": trace_id},
    )
    try:
        upstream = urlopen(request, timeout=35)
        # 普通响应的读取和 JSON 解析也属于上游请求过程。
        if action not in ("send", "subscribe"):
            with upstream:
                result = json.load(upstream)
                if not isinstance(result, dict):
                    raise TypeError("助手响应不是 JSON 对象")
                return result
    except HTTPError as exc:
        # 将 Assistant 的 Problem Details 错误转换为聊天页面使用的错误字段。
        try:
            with exc:
                problem = json.load(exc)
                return {
                    "error": problem.get("detail")
                    or problem.get("title")
                    or "助手请求失败。",
                    "trace_id": problem.get("trace_id"),
                }
        except (ValueError, AttributeError, OSError, HTTPException):
            return {"error": "助手请求失败。"}
    except (URLError, OSError, HTTPException):
        return {"error": "助手服务暂时不可用。"}
    except (ValueError, TypeError):
        return {"error": "助手响应格式异常，请稍后重试。", "trace_id": trace_id}

    # 禁止缓存，并提示支持该响应头的 Nginx 代理不要缓冲流式内容。
    response = Response(
        _stream_response(upstream),
        content_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Trace-ID": trace_id,
        },
    )
    # 浏览器断开或响应关闭时释放上游连接；这不会发送停止 Agent 任务的请求。
    response.call_on_close(upstream.close)
    return response
