import { useSession } from "../useSession";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import { RunControls } from "./RunControls";
import { SessionMenu } from "./SessionMenu";

/** 组织会话菜单、消息、状态与输入区，关闭操作由父组件处理。 */
export function AssistantPanel({ onClose }: { onClose(): void }) {
	const chat = useSession();
	return (
		<aside
			id="buying_ai_panel"
			className="buying-ai-panel"
			aria-label="采购助手对话"
		>
			<header>
				<SessionMenu
					sessions={chat.sessions}
					current={chat.current}
					pending={chat.pending}
					onRefresh={chat.refresh}
					onSelect={chat.select}
					onCreate={chat.create}
					onDelete={chat.remove}
				/>
				<button
					type="button"
					className="btn btn-default btn-sm"
					onClick={onClose}
				>
					关闭
				</button>
			</header>
			<div className="buying-ai-chat-errors">
				{chat.error && (
					<div className="buying-ai-chat-error" role="alert">
						<p>{chat.error}</p>
						<button
							type="button"
							className="btn btn-default btn-sm"
							disabled={chat.loading}
							onClick={() => void chat.retry()}
						>
							重试
						</button>
					</div>
				)}
			</div>
			<MessageList
				messages={chat.messages}
				loading={chat.loading}
				error={chat.error}
			/>
			<p className="buying-ai-chat-status" role="status">
				{chat.status}
			</p>
			<RunControls
				busy={chat.busy}
				resumable={chat.resumable}
				members={chat.members}
				confirmations={chat.confirmations}
				disabled={chat.loading || chat.controlling}
				onResume={chat.resume}
				onCancel={chat.cancel}
				onConfirm={chat.confirm}
			/>
			<Composer
				key={chat.current || "empty"}
				busy={chat.busy}
				disabled={
					!chat.current ||
					chat.loading ||
					chat.controlling ||
					chat.confirmations.length > 0
				}
				uploading={chat.uploading}
				attachments={chat.attachments}
				onSend={chat.send}
				onInterrupt={chat.interrupt}
				onUpload={chat.uploadFiles}
				onRemove={chat.removeAttachment}
			/>
		</aside>
	);
}
