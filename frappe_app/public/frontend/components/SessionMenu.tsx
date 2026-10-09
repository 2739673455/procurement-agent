import { useCallback, useEffect, useRef, useState } from "react";
import type { Session } from "../types";

/** 会话列表、当前选择、操作状态和会话管理回调。 */
interface Props {
	sessions: Session[];
	current: string | null;
	pending: boolean;
	/** 刷新当前用户的会话列表。 */
	onRefresh(): Promise<void>;
	/** 切换到指定会话。 */
	onSelect(id: string): Promise<void>;
	/** 创建并选中新会话。 */
	onCreate(): Promise<void>;
	/** 删除指定会话。 */
	onDelete(id: string): Promise<void>;
}
/** 展示会话选择菜单，支持创建、切换和删除。 */
export function SessionMenu(props: Props) {
	const [open, setOpen] = useState(false);
	const picker = useRef<HTMLDivElement>(null);
	const trigger = useRef<HTMLButtonElement>(null);
	const title =
		props.sessions.find((row) => row.id === props.current)?.title || "新会话";
	/** 收起菜单并将键盘焦点返回标题按钮。 */
	const close = useCallback(() => {
		setOpen(false);
		trigger.current?.focus();
	}, []);
	useEffect(() => {
		if (!open) return;
		/** 点击菜单外部时收起菜单。 */
		function outside(event: PointerEvent) {
			if (!picker.current?.contains(event.target as Node)) setOpen(false);
		}
		/** 按 Esc 收起菜单并恢复按钮焦点。 */
		function onKeyDown(event: KeyboardEvent) {
			if (event.key === "Escape") {
				event.preventDefault();
				close();
			}
		}
		document.addEventListener("pointerdown", outside);
		document.addEventListener("keydown", onKeyDown);
		return () => {
			document.removeEventListener("pointerdown", outside);
			document.removeEventListener("keydown", onKeyDown);
		};
	}, [open, close]);
	return (
		<div
			ref={picker}
			className={`buying-ai-chat-picker${open ? " is-open" : ""}`}
		>
			<button
				ref={trigger}
				type="button"
				className="buying-ai-chat-title btn btn-default"
				title={title}
				aria-expanded={open}
				aria-controls="buying-ai-chat-history"
				onClick={() => {
					setOpen(!open);
					if (!open) void props.onRefresh();
				}}
			>
				<span>{title}</span>
				<span className="buying-ai-chat-chevron">
					<svg
						viewBox="0 0 24 24"
						width="18"
						height="18"
						fill="none"
						stroke="currentColor"
						strokeWidth="2"
						strokeLinecap="round"
						strokeLinejoin="round"
						aria-hidden="true"
					>
						<path d="m6 9 6 6 6-6" />
					</svg>
				</span>
			</button>
			{open && (
				<section
					id="buying-ai-chat-history"
					className="buying-ai-chat-history"
					aria-label="历史会话"
				>
					{!props.sessions.length && <p>暂无历史会话</p>}
					{props.sessions.map((row) => (
						<div className="buying-ai-chat-history-row" key={row.id}>
							<button
								type="button"
								disabled={props.pending}
								className="btn btn-default btn-sm buying-ai-chat-history-title"
								aria-current={row.id === props.current}
								onClick={() => {
									close();
									void props.onSelect(row.id);
								}}
							>
								{row.title}
							</button>
							<button
								type="button"
								disabled={props.pending}
								className="btn btn-default btn-sm"
								aria-label={`删除${row.title}`}
								onClick={() => void props.onDelete(row.id)}
							>
								删除
							</button>
						</div>
					))}
					<button
						type="button"
						disabled={props.pending}
						className="btn btn-default btn-sm buying-ai-chat-create"
						onClick={() => {
							close();
							void props.onCreate();
						}}
					>
						＋ 新建会话
					</button>
				</section>
			)}
		</div>
	);
}
