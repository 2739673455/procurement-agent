/** 与会话接口共享的数据结构，工具结果使用可扩展字段。 */
/** 聊天面板展示的会话摘要。 */
export interface Session { id: string; title: string }
/** 随问题发送的页面路由、表单快照和保存状态。 */
export interface PageContext {
    route: string[];
    doctype?: string;
    name?: string;
    is_new?: boolean;
    is_dirty?: boolean;
    doc?: Record<string, unknown>;
}
/** Frappe 上传结果，name 是文件记录 ID，file_name 是展示名称。 */
export interface Attachment { name: string; file_name: string }
/** 用户、助手或工具的聊天展示记录。 */
export interface Message {
    id?: string;
    role: "user" | "assistant" | "tool";
    content: string;
    result?: unknown;
    page_context?: PageContext | null;
    attachments?: { name: string }[];
}
/** 持久化聊天记录与服务端运行状态。 */
export interface History { messages: Message[]; running: boolean }
/** 界面消费的文本增量、工具状态和运行终态事件。 */
export type ChatEvent =
    | { type: "delta"; delta: string; message_id?: string }
    | { type: "tool_start"; name: string; id: string }
    | { type: "tool_result"; name: string; id: string; result: unknown }
    | { type: "error"; error: string }
    | { type: "stopped" | "done" };
