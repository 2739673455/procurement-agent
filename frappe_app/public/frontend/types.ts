/** 与会话接口共享的数据结构，工具结果使用可扩展字段。 */
export interface Conversation { id: string; title: string }
export interface PageContext {
    route: string[];
    doctype?: string;
    name?: string;
    is_new?: boolean;
    is_dirty?: boolean;
    doc?: Record<string, unknown>;
}
export interface Attachment { name: string; file_name: string }
export interface Message {
    id?: string;
    role: "user" | "assistant" | "tool";
    content: string;
    result?: unknown;
    page_context?: PageContext | null;
    attachments?: { name: string }[];
}
export interface History { messages: Message[]; running: boolean }
export type ChatEvent =
    | { type: "delta"; delta: string; message_id?: string }
    | { type: "tool_start"; name: string; id: string }
    | { type: "tool_result"; name: string; id: string; result: unknown }
    | { type: "error"; error: string }
    | { type: "stopped" | "done" };
