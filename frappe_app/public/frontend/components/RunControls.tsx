import type { RequireUserConfirmEvent } from "@agentscope-ai/agentscope/event";
import type { BackgroundTool, TeamMember } from "../types";

/** 当前会话的运行控制和原生工具确认操作。 */
interface Props {
	sessionId: string | null;
	status: string;
	busy: boolean;
	resumable: boolean;
	members: TeamMember[];
	backgroundTasks: Record<string, BackgroundTool>;
	confirmations: RequireUserConfirmEvent[];
	disabled: boolean;
	onResume(): Promise<void>;
	onInterrupt(): Promise<void>;
	onCancel(): Promise<void>;
	onConfirm(event: RequireUserConfirmEvent, confirmed: boolean): Promise<void>;
}

/** 展示成员 Agent 状态及框架权限请求，不转换聊天消息和事件。 */
export function RunControls(props: Props) {
	const labels: Record<string, string> = {
		idle: "空闲",
		running: "运行中",
		awaiting_permission: "等待确认",
		awaiting_external_result: "等待外部结果",
	};
	return (
		<div className="buying-ai-run-controls">
			{Object.keys(props.backgroundTasks).length > 0 && (
				<ul aria-label="后台工具">
					{Object.entries(props.backgroundTasks).map(([id, task]) => (
						<li key={id}>{task.tool_name}：后台执行中</li>
					))}
				</ul>
			)}
			{props.members.length > 0 && (
				<ul aria-label="协作 Agent">
					{props.members.map((member) => (
						<li key={member.session_id}>
							{member.name}：{labels[member.status] || member.status}
						</li>
					))}
				</ul>
			)}
			{props.confirmations.map((event) => (
				<fieldset key={event.reply_id} aria-label="工具权限确认">
					<p>以下工具需要确认：</p>
					{event.tool_calls.map((call) => (
						<details key={call.id}>
							<summary>{call.name}</summary>
							<pre>{call.input}</pre>
						</details>
					))}
					<button
						type="button"
						className="btn btn-primary btn-sm"
						disabled={props.disabled}
						onClick={() => void props.onConfirm(event, true)}
					>
						允许本次调用
					</button>
					<button
						type="button"
						className="btn btn-default btn-sm"
						disabled={props.disabled}
						onClick={() => void props.onConfirm(event, false)}
					>
						拒绝
					</button>
				</fieldset>
			))}
			{props.resumable && (
				<button
					type="button"
					className="btn btn-primary btn-sm"
					disabled={props.disabled}
					onClick={() => void props.onResume()}
				>
					继续执行
				</button>
			)}
			{!props.busy &&
				(props.status === "awaiting_permission" ||
					props.status === "awaiting_external_result") && (
					<button
						type="button"
						className="btn btn-default btn-sm"
						disabled={props.disabled}
						onClick={() => void props.onInterrupt()}
					>
						中断当前回复
					</button>
				)}
			{(props.busy ||
				Object.values(props.backgroundTasks).some(
					(task) => task.session_id === props.sessionId,
				)) && (
				<button
					type="button"
					className="btn btn-default btn-sm"
					disabled={props.disabled}
					onClick={() => void props.onCancel()}
				>
					取消当前运行
				</button>
			)}
		</div>
	);
}
