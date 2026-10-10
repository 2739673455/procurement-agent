import {
	EventType,
	ReplyFinishedReason,
	type RequireUserConfirmEvent,
} from "@agentscope-ai/agentscope/event";
import {
	AssistantMsg,
	appendEvent,
	type Msg,
	UserMsg,
} from "@agentscope-ai/agentscope/message";
import { useEffect, useEffectEvent, useRef, useState } from "react";
import { api, events, upload } from "./api";
import { frappe, pageSnapshot } from "./frappe";
import type {
	Attachment,
	BackgroundTool,
	History,
	PageContext,
	Session,
	TeamMember,
} from "./types";

/** 管理会话状态；关闭或切换时中止订阅，不停止服务端正在执行的任务。 */
export function useSession() {
	const storageKey = `buying-ai-session:${frappe.session.user}`;
	const [sessions, setSessions] = useState<Session[]>([]);
	const [current, setCurrent] = useState<string | null>(null);
	const [messages, setMessages] = useState<Msg[]>([]);
	const [attachments, setAttachments] = useState<Attachment[]>([]);
	const [busy, setBusy] = useState(false);
	const [resumable, setResumable] = useState(false);
	const [members, setMembers] = useState<TeamMember[]>([]);
	const [backgroundTasks, setBackgroundTasks] = useState<
		Record<string, BackgroundTool>
	>({});
	const [confirmations, setConfirmations] = useState<RequireUserConfirmEvent[]>(
		[],
	);
	const [controlling, setControlling] = useState(false);
	const controlPending = useRef(false);
	const [loading, setLoading] = useState(true);
	const [uploading, setUploading] = useState(false);
	const [pending, setPending] = useState(false);
	const [status, setStatus] = useState("");
	const [error, setError] = useState("");
	const currentRef = useRef<string | null>(null);
	const view = useRef(new AbortController());
	const lifetime = useRef(new AbortController());
	const mutation = useRef(false);
	const sending = useRef(false);
	const listVersion = useRef(0);
	const connected = useRef(false);
	const running = useRef(false);
	const revision = useRef(0);
	const checking = useRef(false);

	/** 展示未取消请求的错误，忽略主动中止带来的错误。 */
	function report(cause: unknown, signal: AbortSignal) {
		if (!signal.aborted)
			setError(cause instanceof Error ? cause.message : "请求失败。");
	}
	/** 同步当前会话引用、界面状态和按登录用户区分的本地选择。 */
	function remember(id: string | null) {
		currentRef.current = id;
		setCurrent(id);
		if (id) localStorage.setItem(storageKey, id);
		else localStorage.removeItem(storageKey);
	}
	/** 刷新会话列表，避免较早发出的请求覆盖较新的响应。 */
	async function list(signal: AbortSignal) {
		const version = ++listVersion.current;
		const data = await api.list(signal);
		if (!signal.aborted && version === listVersion.current)
			setSessions(data.sessions);
		return data.sessions;
	}
	/** 使用框架状态更新任务控制和 Agent 团队成员。 */
	function applyState(history: History) {
		running.current = history.running;
		setBusy(history.running);
		setResumable(history.resumable);
		setMembers(history.members);
		setBackgroundTasks(history.background_tasks);
		setConfirmations(history.confirmations);
	}
	/** 同步运行状态，空闲时以持久化消息为准；运行中的流式消息留在界面。 */
	async function reconcile(id: string, signal: AbortSignal) {
		if (checking.current || sending.current || signal.aborted) return;
		checking.current = true;
		const version = revision.current;
		try {
			const history = await api.history(id, signal);
			if (signal.aborted || version !== revision.current || sending.current)
				return;
			applyState(history);
			if (!history.running) {
				setMessages(history.messages);
				const last = history.messages[history.messages.length - 1];
				setStatus(
					history.intent === "cancelled"
						? "任务已取消。"
						: history.resumable
							? "可从保存的上下文继续。"
							: "",
				);
				if (last?.finished_reason === ReplyFinishedReason.ERROR)
					setError("执行失败或超时，请稍后重试。");
				if (last?.finished_reason === ReplyFinishedReason.EXCEED_MAX_ITERS)
					setError("工具调用已达到上限，请缩小问题范围后重试。");
			}
		} finally {
			checking.current = false;
		}
	}
	/** 每个选中会话保持一条订阅，回复结束后继续接收后续运行事件。 */
	async function stream(id: string, signal: AbortSignal, onReady: () => void) {
		const seen = new Set<string>();
		// 运行期间核对持久化状态，覆盖快速结束、事件空窗和 REPLY_END 后的保存阶段。
		const timer = window.setInterval(() => {
			if (connected.current && running.current)
				void reconcile(id, signal).catch((cause) => report(cause, signal));
		}, 1500);
		try {
			await events(
				id,
				signal,
				(event) => {
					if (signal.aborted || seen.has(event.id)) return;
					seen.add(event.id);
					if ("reply_id" in event) {
						setMessages((previous) => {
							const index = previous.findIndex(
								(message) => message.id === event.reply_id,
							);
							if (event.type === EventType.REPLY_START && index < 0) {
								return [
									...previous,
									AssistantMsg({
										id: event.reply_id,
										name: event.name,
										content: [],
										created_at: event.created_at,
									}),
								];
							}
							if (index < 0) return previous;
							return previous.map((message, position) =>
								position === index && !message.finished_at
									? appendEvent(structuredClone(message), event)
									: message,
							);
						});
					}
					switch (event.type) {
						case EventType.REPLY_START:
							revision.current++;
							running.current = true;
							setBusy(true);
							setError("");
							setStatus("正在生成…");
							break;
						case EventType.TOOL_RESULT_START:
							setStatus(`正在执行工具：${event.tool_call_name}…`);
							break;
						case EventType.TOOL_RESULT_END:
							setStatus("正在生成…");
							break;
						case EventType.REQUIRE_USER_CONFIRM:
						case EventType.CUSTOM:
							void reconcile(id, signal).catch((cause) =>
								report(cause, signal),
							);
							break;
						case EventType.REPLY_END:
							if (event.finished_reason === ReplyFinishedReason.INTERRUPTED)
								setStatus("已中断，可从保存的上下文继续。");
							if (event.finished_reason === ReplyFinishedReason.ERROR)
								setError("执行失败或超时，请稍后重试。");
							if (
								event.finished_reason === ReplyFinishedReason.EXCEED_MAX_ITERS
							)
								setError("工具调用已达到上限，请缩小问题范围后重试。");
							void reconcile(id, signal).catch((cause) =>
								report(cause, signal),
							);
							break;
					}
				},
				() => {
					if (!signal.aborted) {
						connected.current = true;
						onReady();
					}
				},
			);
			if (!signal.aborted)
				throw new Error("订阅连接已关闭，请重试以恢复连接。");
		} catch (cause) {
			report(cause, signal);
			throw cause;
		} finally {
			window.clearInterval(timer);
			if (!signal.aborted) connected.current = false;
		}
	}
	/** 中止当前订阅并切换会话，读取历史后建立持续订阅。 */
	async function load(id: string | null) {
		view.current.abort();
		const controller = new AbortController();
		view.current = controller;
		const { signal } = controller;
		sending.current = false;
		connected.current = false;
		running.current = false;
		revision.current++;
		remember(id);
		setMessages([]);
		setAttachments([]);
		setUploading(false);
		setLoading(Boolean(id));
		setBusy(false);
		setResumable(false);
		setMembers([]);
		setBackgroundTasks({});
		setConfirmations([]);
		setError("");
		setStatus("");
		if (!id) return;
		try {
			const history = await api.history(id, signal);
			if (signal.aborted) return;
			setMessages(history.messages);
			applyState(history);
			await new Promise<void>((resolve, reject) => {
				void stream(id, signal, resolve).catch(reject);
			});
			if (!signal.aborted) await reconcile(id, signal);
		} catch (cause) {
			report(cause, signal);
		} finally {
			if (!signal.aborted) setLoading(false);
		}
	}
	/** 刷新会话菜单，错误展示到面板。 */
	async function refresh() {
		const signal = lifetime.current.signal;
		try {
			await list(signal);
		} catch (cause) {
			report(cause, signal);
		}
	}
	/** 恢复选中会话和订阅，同一会话保留未发送附件。 */
	async function retry() {
		const signal = view.current.signal;
		setLoading(true);
		try {
			const rows = await list(signal);
			if (signal.aborted) return;
			const preferred = currentRef.current || localStorage.getItem(storageKey);
			const id =
				rows.find((row) => row.id === preferred)?.id || rows[0]?.id || null;
			// 同一会话重连时保留未发送附件。
			const pendingAttachments = attachments;
			const same = id === currentRef.current;
			const resumed = load(id);
			if (same) setAttachments(pendingAttachments);
			await resumed;
		} catch (cause) {
			report(cause, signal);
		} finally {
			if (!signal.aborted) setLoading(false);
		}
	}
	/** 协调会话创建和删除操作，阻止操作期间的重复提交。 */
	async function mutate(action: (signal: AbortSignal) => Promise<void>) {
		if (mutation.current) return;
		mutation.current = true;
		setPending(true);
		const signal = lifetime.current.signal;
		try {
			await action(signal);
		} catch (cause) {
			report(cause, signal);
		} finally {
			mutation.current = false;
			if (!signal.aborted) setPending(false);
		}
	}
	/** 创建并选中新会话，随后刷新会话列表。 */
	async function create() {
		await mutate(async (signal) => {
			const row = await api.create(signal);
			if (signal.aborted) return;
			void load(row.id);
			await list(signal);
		});
	}
	/** 确认后删除会话，删除当前会话时选择剩余会话。 */
	async function remove(id: string) {
		if (!window.confirm("删除此会话及其历史？正在执行的任务也会停止。")) return;
		await mutate(async (signal) => {
			await api.remove(id, signal);
			if (signal.aborted) return;
			const wasCurrent = currentRef.current === id;
			if (wasCurrent) void load(null);
			const rows = await list(signal);
			if (!signal.aborted && wasCurrent) void load(rows[0]?.id || null);
		});
	}
	/** 携带表单快照和附件启动后台运行，服务端接受后清空待发送附件。 */
	async function send(text: string, includeContext: boolean) {
		const id = currentRef.current;
		if (
			!id ||
			busy ||
			loading ||
			uploading ||
			controlling ||
			confirmations.length > 0 ||
			sending.current ||
			!text.trim()
		)
			return false;
		const signal = view.current.signal;
		if (!connected.current) {
			setError("请重试以建立事件订阅后发送。");
			return false;
		}
		let context: PageContext | null;
		try {
			context = includeContext ? pageSnapshot() : null;
		} catch (cause) {
			report(cause, signal);
			return false;
		}
		sending.current = true;
		revision.current++;
		running.current = true;
		setError("");
		setBusy(true);
		setStatus("正在生成…");
		setMessages((previous) => [
			...previous,
			UserMsg({
				name: "user",
				content: text.trim(),
				metadata: {
					display_text: text.trim(),
					page_context: context
						? {
								route: context.route,
								is_new: Boolean(context.is_new),
								is_dirty: Boolean(context.is_dirty),
							}
						: null,
					attachments: attachments.map((file) => ({ name: file.name })),
				},
			}),
		]);
		try {
			await api.send(
				id,
				{
					message: text.trim(),
					page_context: context,
					attachments: attachments.map((file) => file.name),
				},
				signal,
			);
			if (signal.aborted) return false;
			setAttachments([]);
			return true;
		} catch (cause) {
			report(cause, signal);
			return false;
		} finally {
			if (!signal.aborted) {
				sending.current = false;
				void reconcile(id, signal).catch((cause) => report(cause, signal));
			}
		}
	}
	/** 串行提交任务控制请求，并重新读取持久化状态。 */
	async function control(
		action: (id: string, signal: AbortSignal) => Promise<unknown>,
	) {
		const id = currentRef.current;
		const signal = view.current.signal;
		if (!id || controlPending.current) return;
		controlPending.current = true;
		setControlling(true);
		try {
			await action(id, signal);
			await reconcile(id, signal);
		} catch (cause) {
			report(cause, signal);
		} finally {
			controlPending.current = false;
			if (!signal.aborted) setControlling(false);
		}
	}
	/** 使用原生调用信息确认或拒绝本次权限请求。 */
	async function confirm(event: RequireUserConfirmEvent, confirmed: boolean) {
		await control((id, signal) =>
			api.confirm(
				id,
				{
					type: EventType.USER_CONFIRM_RESULT,
					id: crypto.randomUUID(),
					created_at: new Date().toISOString(),
					reply_id: event.reply_id,
					confirm_results: event.tool_calls.map((tool_call) => ({
						confirmed,
						tool_call,
						rules: null,
					})),
				},
				signal,
			),
		);
	}
	/** 依次上传文件并更新上传状态和待发送附件列表。 */
	async function uploadFiles(files: File[]) {
		const id = currentRef.current;
		if (!id) return;
		const signal = view.current.signal;
		setUploading(true);
		try {
			for (const file of files) {
				setStatus(`正在上传：${file.name}`);
				const attachment = await upload(id, file, signal);
				if (signal.aborted) return;
				setAttachments((previous) => [
					...previous.filter((file) => file.name !== attachment.name),
					attachment,
				]);
			}
		} catch (cause) {
			report(cause, signal);
		} finally {
			if (!signal.aborted) {
				setUploading(false);
				setStatus("");
			}
		}
	}
	// 面板挂载时恢复会话；后续状态更新不重新初始化订阅。
	const restore = useEffectEvent(retry);
	useEffect(() => {
		lifetime.current = new AbortController();
		view.current = new AbortController();
		void restore();
		return () => {
			lifetime.current.abort();
			view.current.abort();
		};
	}, []);
	return {
		sessions,
		current,
		messages,
		attachments,
		busy,
		loading,
		uploading,
		pending,
		status,
		error,
		resumable,
		members,
		backgroundTasks,
		confirmations,
		controlling,
		confirm,
		interrupt: () => control(api.interrupt),
		resume: () => control(api.resume),
		cancel: () => control(api.cancel),
		select: load,
		refresh,
		retry,
		create,
		remove,
		send,
		uploadFiles,
		/** 按文件名移除待发送附件，沙箱中的文件保留。 */
		removeAttachment: (name: string) =>
			setAttachments((previous) =>
				previous.filter((file) => file.name !== name),
			),
	};
}
