import { ReplyFinishedReason } from "@agentscope-ai/agentscope/event";
import type {
	DataBlock,
	Msg,
	ToolResultBlock,
} from "@agentscope-ai/agentscope/message";
import { useEffect, useRef } from "react";
import { frappe } from "../frappe";

/** 将工具返回值转换为可展示的文本。 */
function display(value: unknown): string {
	return typeof value === "string" ? value : (JSON.stringify(value) ?? "");
}
/** 判断结果是否为非空对象，供工具结果展示读取字段。 */
function record(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
}
/** 展示物料查询结果和跳转入口，其他结果按文本显示。 */
function ItemResults({ result }: { result: unknown }) {
	if (!record(result) || !Array.isArray(result.items)) {
		return (
			<p>
				{display(
					record(result) ? result.error || result.content || result : result,
				)}
			</p>
		);
	}
	return (
		<>
			<p>
				{display(
					result.error ||
						`返回 ${result.items.length} 条物料，起始位置 ${result.offset || 0}${result.has_more ? "，还有更多结果" : ""}`,
				)}
			</p>
			{result.items.filter(record).map((item, index) => (
				<button
					type="button"
					className="btn btn-default btn-sm"
					key={String(item.name || index)}
					onClick={() => frappe.set_route("Form", "Item", String(item.name))}
				>
					{display(item.item_code || item.name)} ·{" "}
					{display(item.item_name || "查看物料")}
				</button>
			))}
		</>
	);
}
/** 直接展示原生图片内容块，其他数据类型显示附件名称。 */
function DataContent({ block }: { block: DataBlock }) {
	const source = block.source;
	if (!source.media_type.startsWith("image/"))
		return (
			<small className="buying-ai-chat-message-meta">
				附件：{block.name || source.media_type}
			</small>
		);
	const url =
		source.type === "base64"
			? `data:${source.media_type};base64,${source.data}`
			: source.url;
	return (
		<img
			className="buying-ai-chat-data-image"
			src={url}
			alt={block.name || "图片"}
			loading="lazy"
		/>
	);
}
/** 渲染原生工具结果中的文本与图片，物料查询使用业务展示组件。 */
function ToolResult({ block }: { block: ToolResultBlock }) {
	const output = block.output;
	const text =
		typeof output === "string"
			? output
			: output
					.filter((item) => item.type === "text")
					.map((item) => item.text)
					.join("\n");
	let result: unknown = text;
	if (block.name === "query_items") {
		try {
			result = JSON.parse(text);
		} catch {
			/* 非 JSON 结果按文本展示。 */
		}
	}
	return (
		<div className="buying-ai-chat-tool-result">
			<strong>
				工具：{block.name}
				{block.state === "running" ? "（执行中）" : ""}
			</strong>
			{block.name === "query_items" ? (
				<ItemResults result={result} />
			) : (
				text && <p>{text}</p>
			)}
			{typeof output !== "string" &&
				output
					.filter((item) => item.type === "data")
					.map((item) => <DataContent key={item.id} block={item} />)}
		</div>
	);
}
/** 展示聊天记录及附件摘要，并在消息更新时滚动到底部。 */
export function MessageList({
	messages,
	loading,
	error,
}: {
	messages: Msg[];
	loading: boolean;
	error: string;
}) {
	const log = useRef<HTMLDivElement>(null);
	useEffect(() => {
		if (log.current) log.current.scrollTop = log.current.scrollHeight;
	}, [messages]);
	return (
		<div className="buying-ai-chat-log" role="log" ref={log}>
			{!messages.length && !error && (
				<div className="buying-ai-chat-empty">
					{loading ? "正在加载…" : "有什么可以帮你？"}
				</div>
			)}
			{messages
				.filter((message) => message.role !== "system")
				.map((message) => {
					const context = message.metadata.page_context;
					const files = message.metadata.attachments;
					const displayText = message.metadata.display_text;
					return (
						<article
							className={`buying-ai-chat-message ${message.role}`}
							key={message.id}
						>
							<strong>{message.role === "user" ? "你" : "采购助手"}</strong>
							{message.role === "user" && typeof displayText === "string" ? (
								<p>{displayText}</p>
							) : (
								message.content.map((block) =>
									block.type === "text" ? (
										<p key={block.id}>{block.text}</p>
									) : block.type === "tool_result" ? (
										<ToolResult key={block.id} block={block} />
									) : null,
								)
							)}
							{message.content
								.filter((block) => block.type === "data")
								.map((block) => (
									<DataContent key={block.id} block={block} />
								))}
							{record(context) && Array.isArray(context.route) && (
								<small className="buying-ai-chat-message-meta">
									页面：
									{context.route
										.filter((item) => typeof item === "string")
										.join(" / ")}
									{Boolean(context.is_new || context.is_dirty) &&
										"（发送时未保存）"}
								</small>
							)}
							{Array.isArray(files) &&
								files.map(
									(file, index) =>
										record(file) && (
											<small
												key={index}
												className="buying-ai-chat-message-meta"
											>
												附件：{display(file.name)}
											</small>
										),
								)}
							{message.finished_reason === ReplyFinishedReason.ERROR && (
								<small className="buying-ai-chat-message-meta">
									回复失败，请稍后重试。
								</small>
							)}
							{message.finished_reason === ReplyFinishedReason.INTERRUPTED && (
								<small className="buying-ai-chat-message-meta">
									已停止生成。
								</small>
							)}
							{message.finished_reason ===
								ReplyFinishedReason.EXCEED_MAX_ITERS && (
								<small className="buying-ai-chat-message-meta">
									工具调用已达到上限。
								</small>
							)}
						</article>
					);
				})}
		</div>
	);
}
