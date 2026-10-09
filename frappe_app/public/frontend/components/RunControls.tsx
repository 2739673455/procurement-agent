import type { RequireUserConfirmEvent } from "@agentscope-ai/agentscope/event";
import type { TeamMember } from "../types";

/** 当前任务的恢复、取消和原生工具确认操作。 */
interface Props {
	busy: boolean;
	resumable: boolean;
	members: TeamMember[];
	confirmations: RequireUserConfirmEvent[];
	disabled: boolean;
	onResume(): Promise<void>;
	onCancel(): Promise<void>;
	onConfirm(event: RequireUserConfirmEvent, confirmed: boolean): Promise<void>;
}

/** 展示团队成员状态及框架权限请求，不转换聊天消息和事件。 */
export function RunControls(props: Props) {
	const labels: Record<string, string> = {
		idle: "空闲",
		running: "运行中",
		awaiting_permission: "等待确认",
		awaiting_external_result: "等待外部结果",
	};
	return (
		<div className="buying-ai-run-controls">
			{props.members.length > 0 && (
				<ul aria-label="团队成员">
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
			{(props.busy || props.resumable || props.confirmations.length > 0) && (
				<button
					type="button"
					className="btn btn-default btn-sm"
					disabled={props.disabled}
					onClick={() => void props.onCancel()}
				>
					取消任务
				</button>
			)}
		</div>
	);
}
