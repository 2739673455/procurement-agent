import { frappe } from "./frappe";
import type { Attachment, ChatEvent, Conversation, History, PageContext } from "./types";

async function request(action: string, data: object, signal: AbortSignal): Promise<Response> {
    const response = await fetch("/api/method/buying_ai.api.conversations", {
        method: "POST", credentials: "same-origin", signal,
        headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token },
        body: JSON.stringify({ action, ...data }),
    });
    return response;
}
async function json<T>(response: Response): Promise<T> {
    const body = await response.json();
    const result = body.message || body;
    if (!response.ok || result.error) throw new Error(result.error || "请求失败，请检查登录状态。");
    return result as T;
}
async function command<T>(action: string, data: object, signal: AbortSignal): Promise<T> {
    return json<T>(await request(action, data, signal));
}
export const api = {
    list: (signal: AbortSignal) => command<{ conversations: Conversation[] }>("list", {}, signal),
    create: (signal: AbortSignal) => command<Conversation>("create", {}, signal),
    remove: (id: string, signal: AbortSignal) => command("delete", { conversation_id: id }, signal),
    history: (id: string, signal: AbortSignal) => command<History>("messages", { conversation_id: id }, signal),
    stop: (id: string, signal: AbortSignal) => command("stop", { conversation_id: id }, signal),
};
export interface Turn { message: string; page_context: PageContext | null; attachments: string[] }

/** 按帧解析 SSE，支持跨网络分块的 UTF-8、多行 data 和心跳。 */
export async function events(id: string, turn: Turn | undefined, signal: AbortSignal, onEvent: (event: ChatEvent) => void): Promise<void> {
    const response = await request(turn ? "send" : "subscribe", { conversation_id: id, ...turn }, signal);
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
