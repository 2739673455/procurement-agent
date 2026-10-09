import type { RequireUserConfirmEvent } from "@agentscope-ai/agentscope/event";
import type { Msg } from "@agentscope-ai/agentscope/message";

/** 会话业务数据；聊天消息和事件使用官方 SDK 类型。 */
/** 聊天面板展示的会话摘要。 */
export interface Session {
	id: string;
	title: string;
}
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
export interface Attachment {
	name: string;
	file_name: string;
}
/** 持久化聊天记录与服务端运行状态。 */
export interface History {
	messages: Msg[];
	running: boolean;
	resumable: boolean;
	status: string;
	intent: string;
	members: TeamMember[];
	confirmations: RequireUserConfirmEvent[];
}
/** 框架团队内成员会话的运行摘要。 */
export interface TeamMember {
	agent_id: string;
	session_id: string;
	name: string;
	status: string;
}
