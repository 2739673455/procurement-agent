import { frappe } from "./frappe";
import type { Attachment, ChatEvent, Session, History, PageContext } from "./types";

/** 通过 Frappe 同源入口发送会话操作，携带 CSRF 标识并支持取消请求。 */
async function request(action: string, data: object, signal: AbortSignal): Promise<Response> {
    const response = await fetch("/api/method/buying_ai.api.sessions", {
        method: "POST", credentials: "same-origin", signal,
        headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token },
        body: JSON.stringify({ action, ...data }),
    });
    return response;
}
/** 解析 Frappe 响应包装，统一处理 HTTP 与业务错误。 */
async function json<T>(response: Response): Promise<T> {
    const body = await response.json();
    const result = body.message || body;
    if (!response.ok || result.error) throw new Error(result.error || "请求失败，请检查登录状态。");
    return result as T;
}
/** 发送并解析返回 JSON 的会话操作。 */
async function command<T>(action: string, data: object, signal: AbortSignal): Promise<T> {
    return json<T>(await request(action, data, signal));
}
/** 返回 JSON 的会话操作集合，发送和订阅由 events 处理。 */
export const api = {
    /** 读取当前用户的会话列表。 */
    list: (signal: AbortSignal) => command<{ sessions: Session[] }>("list", {}, signal),
    /** 创建会话并返回服务端生成的 ID。 */
    create: (signal: AbortSignal) => command<Session>("create", {}, signal),
    /** 删除指定会话及其数据，并停止正在执行的任务。 */
    remove: (id: string, signal: AbortSignal) => command("delete", { session_id: id }, signal),
    /** 读取会话消息及运行状态。 */
    history: (id: string, signal: AbortSignal) => command<History>("messages", { session_id: id }, signal),
    /** 停止指定会话的当前运行。 */
    stop: (id: string, signal: AbortSignal) => command("stop", { session_id: id }, signal),
};
/** 单轮发送内容，attachments 保存 Frappe 附件 ID。 */
export interface Turn { message: string; page_context: PageContext | null; attachments: string[] }

/** 按帧解析 SSE，支持跨网络分块的 UTF-8、多行 data 和心跳。 */
export async function events(id: string, turn: Turn | undefined, signal: AbortSignal, onEvent: (event: ChatEvent) => void): Promise<void> {
    const response = await request(turn ? "send" : "subscribe", { session_id: id, ...turn }, signal);
    if (!response.ok || !response.headers.get("content-type")?.includes("text/event-stream")) {
        await json(response);
        throw new Error("未收到对话事件流。");
    }
    if (!response.body) throw new Error("未收到对话事件流。");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
        while (true) {
            const chunk = await reader.read();
            buffer += decoder.decode(chunk.value, { stream: !chunk.done });
            let boundary: RegExpExecArray | null;
            while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
                const frame = buffer.slice(0, boundary.index);
                buffer = buffer.slice(boundary.index + boundary[0].length);
                const data = frame.split(/\r?\n/).filter(line => line.startsWith("data:"))
                    .map(line => line.slice(5).replace(/^ /, "")).join("\n");
                if (data && !signal.aborted) onEvent(JSON.parse(data) as ChatEvent);
            }
            if (chunk.done) break;
        }
    } finally {
        await reader.cancel().catch(() => undefined);
        reader.releaseLock();
    }
}
/** 上传站点私有附件，返回 Frappe 文件记录标识与文件名。 */
export async function upload(file: File, signal: AbortSignal): Promise<Attachment> {
    const body = new FormData();
    body.append("file", file);
    body.append("is_private", "1");
    const response = await fetch("/api/method/upload_file", {
        method: "POST", credentials: "same-origin", signal,
        headers: { "X-Frappe-CSRF-Token": frappe.csrf_token }, body,
    });
    const result = await json<Attachment>(response);
    if (!result.name) throw new Error(`附件上传失败：${file.name}`);
    return result;
}
