import { useEffect, useRef } from "react";
import { frappe } from "../frappe";
import type { Message } from "../types";

function display(value: unknown): string { return typeof value === "string" ? value : JSON.stringify(value) ?? ""; }
function record(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null; }
function ToolResult({ result }: { result: unknown }) {
    if (!record(result) || !Array.isArray(result.items)) {
        return <p>{display(record(result) ? result.error || result.content || result : result)}</p>;
    }
    return <>
        <p>{display(result.error || `返回 ${result.items.length} 条物料，起始位置 ${result.offset || 0}${result.has_more ? "，还有更多结果" : ""}`)}</p>
        {result.items.filter(record).map((item, index) => <button type="button" className="btn btn-default btn-sm" key={String(item.name || index)}
            onClick={() => frappe.set_route("Form", "Item", String(item.name))}>
            {display(item.item_code || item.name)} · {display(item.item_name || "查看物料")}
        </button>)}
    </>;
}
export function MessageList({ messages, loading, error }: { messages: Message[]; loading: boolean; error: string }) {
    const log = useRef<HTMLDivElement>(null);
    useEffect(() => { if (log.current) log.current.scrollTop = log.current.scrollHeight; }, [messages]);
    return <div className="buying-ai-chat-log" role="log" ref={log}>
        {!messages.length && !error && <div className="buying-ai-chat-empty">{loading ? "正在加载…" : "有什么可以帮你？"}</div>}
        {messages.map((message, index) => <article className={`buying-ai-chat-message ${message.role}`} key={message.id || index}>
            <strong>{message.role === "user" ? "你" : message.role === "tool" ? "工具结果" : "采购助手"}</strong>
            {message.role === "tool" ? <ToolResult result={message.result} /> : <p>{message.content}</p>}
            {message.page_context && <small className="buying-ai-chat-message-meta">页面：{message.page_context.route.join(" / ")}
                {(message.page_context.is_new || message.page_context.is_dirty) && "（发送时未保存）"}</small>}
            {message.attachments?.map((file, index) => <small key={index} className="buying-ai-chat-message-meta">附件：{file.name}</small>)}
        </article>)}
    </div>;
}
