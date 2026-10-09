const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const {
	appendEvent,
	AssistantMsg,
	UserMsg,
} = require("@agentscope-ai/agentscope/message");

// 使用项目 TypeScript 源码测试渲染，无需生成或提交构建产物。
for (const extension of [".ts", ".tsx"]) {
	require.extensions[extension] = (module, filename) => {
		const result = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
			compilerOptions: {
				module: ts.ModuleKind.CommonJS,
				jsx: ts.JsxEmit.ReactJSX,
			},
		});
		module._compile(result.outputText, filename);
	};
}
global.window = { frappe: { csrf_token: "test" } };
const {
	MessageList,
} = require("../public/frontend/components/MessageList.tsx");
const { api, events: subscribe } = require("../public/frontend/api.ts");

test("SSE 分块中的原生事件经 SDK 累积后渲染文本、物料结果和工具图片", async () => {
	const message = AssistantMsg({ id: "reply", name: "assistant", content: [] });
	const events = [
		{ type: "TEXT_BLOCK_START", block_id: "text" },
		{ type: "TEXT_BLOCK_DELTA", block_id: "text", delta: "找到物料" },
		{
			type: "TOOL_RESULT_START",
			tool_call_id: "tool",
			tool_call_name: "query_items",
		},
		{
			type: "TOOL_RESULT_TEXT_DELTA",
			tool_call_id: "tool",
			delta: '{"items":[{"name":"BOLT","item_name":"螺丝"}]}',
		},
		{
			type: "TOOL_RESULT_DATA_DELTA",
			tool_call_id: "tool",
			block_id: "image",
			data: "cG5n",
			media_type: "image/png",
		},
		{
			type: "TOOL_RESULT_END",
			tool_call_id: "tool",
			state: "success",
			metadata: {},
		},
		{ type: "REPLY_END", session_id: "session", finished_reason: "completed" },
	];
	const frames = events.map(
		(event) =>
			"data: " +
			JSON.stringify({
				id: crypto.randomUUID(),
				created_at: new Date().toISOString(),
				reply_id: "reply",
				...event,
			}) +
			"\r\n\r\n",
	);
	const data = new TextEncoder().encode(
		": heartbeat\r\n\r\n" + frames.join(""),
	);
	const originalFetch = global.fetch;
	global.fetch = async (_url, options) => {
		assert.equal(JSON.parse(options.body).action, "subscribe");
		let position = 0;
		return new Response(
			new ReadableStream({
				pull(controller) {
					if (position === data.length) controller.close();
					else controller.enqueue(data.slice(position, ++position));
				},
			}),
			{ headers: { "content-type": "text/event-stream" } },
		);
	};
	try {
		await subscribe("session", new AbortController().signal, (event) =>
			appendEvent(message, event),
		);
	} finally {
		global.fetch = originalFetch;
	}
	const html = renderToStaticMarkup(
		React.createElement(MessageList, {
			messages: [message],
			loading: false,
			error: "",
		}),
	);
	assert.match(html, /找到物料/);
	assert.match(html, /BOLT/);
	assert.match(html, /螺丝/);
	assert.match(html, /data:image\/png;base64,cG5n/);
	assert.equal(message.finished_reason, "completed");
});

test("原生消息直接展示业务元数据和失败终态，隐藏思考及模型输入快照", () => {
	const user = UserMsg({
		name: "user",
		content: "包含表单的模型输入",
		metadata: {
			display_text: "看看附件",
			page_context: { route: ["Form", "Item", "BOLT"], is_dirty: true },
			attachments: [{ name: "notes.txt" }],
		},
	});
	const assistant = AssistantMsg({
		name: "assistant",
		content: [
			{
				type: "thinking",
				id: "thinking",
				thinking: "内部思考",
				created_at: new Date().toISOString(),
			},
		],
	});
	assistant.finished_reason = "error";
	const html = renderToStaticMarkup(
		React.createElement(MessageList, {
			messages: JSON.parse(JSON.stringify([user, assistant])),
			loading: false,
			error: "",
		}),
	);
	assert.match(html, /看看附件/);
	assert.match(html, /Form \/ Item \/ BOLT/);
	assert.match(html, /notes.txt/);
	assert.match(html, /回复失败/);
	assert.doesNotMatch(html, /内部思考|包含表单的模型输入/);
});

test("发送返回启动结果，两次运行使用同一条持续订阅", async () => {
	const originalFetch = global.fetch;
	let source;
	let subscriptions = 0;
	let sends = 0;
	global.fetch = async (_url, options) => {
		const request = JSON.parse(options.body);
		if (request.action === "send") {
			sends++;
			return new Response(
				JSON.stringify({
					message: { status: "started", session_id: request.session_id },
				}),
				{ headers: { "content-type": "application/json" } },
			);
		}
		assert.equal(request.action, "subscribe");
		subscriptions++;
		return new Response(
			new ReadableStream({
				start(controller) {
					source = controller;
				},
			}),
			{ headers: { "content-type": "text/event-stream" } },
		);
	};
	let ready;
	const connected = new Promise((resolve) => {
		ready = resolve;
	});
	let received;
	const replies = [];
	let ended = false;
	const signal = new AbortController().signal;
	const stream = subscribe(
		"session",
		signal,
		(event) => {
			replies.push(event);
			received();
		},
		ready,
	).then(() => {
		ended = true;
	});
	try {
		await connected;
		for (const reply of ["first", "second"]) {
			assert.deepEqual(
				await api.send(
					"session",
					{ message: "问题", page_context: null, attachments: [] },
					signal,
				),
				{ status: "started", session_id: "session" },
			);
			const delivered = new Promise((resolve) => {
				received = resolve;
			});
			source.enqueue(
				new TextEncoder().encode(
					"data: " +
						JSON.stringify({
							id: reply,
							type: "REPLY_END",
							reply_id: reply,
							session_id: "session",
							created_at: new Date().toISOString(),
							finished_reason: "completed",
						}) +
						"\n\n",
				),
			);
			await delivered;
			assert.equal(ended, false);
		}
		assert.equal(subscriptions, 1);
		assert.equal(sends, 2);
		assert.equal(replies.length, 2);
	} finally {
		source.close();
		await stream;
		global.fetch = originalFetch;
	}
});

test("运行控制提交独立动作和原生确认结果，界面显示恢复及成员权限请求", async () => {
	const {
		RunControls,
	} = require("../public/frontend/components/RunControls.tsx");
	const confirmation = {
		type: "REQUIRE_USER_CONFIRM",
		reply_id: "reply",
		tool_calls: [
			{
				type: "tool_call",
				id: "call",
				name: "Bash",
				input: '{"command":"echo hello"}',
				state: "asking",
			},
		],
	};
	const result = {
		type: "USER_CONFIRM_RESULT",
		reply_id: "reply",
		confirm_results: [
			{ confirmed: true, tool_call: confirmation.tool_calls[0], rules: null },
		],
	};
	const originalFetch = global.fetch;
	const requests = [];
	global.fetch = async (_url, options) => {
		requests.push(JSON.parse(options.body));
		return new Response(JSON.stringify({ message: { ok: true } }));
	};
	try {
		const signal = new AbortController().signal;
		await api.interrupt("session", signal);
		await api.resume("session", signal);
		await api.cancel("session", signal);
		await api.confirm("session", result, signal);
		assert.deepEqual(
			requests.map((row) => row.action),
			["interrupt", "resume", "cancel", "confirm"],
		);
		assert.deepEqual(requests[3].confirmation, result);
		assert.ok(requests.every((row) => row.session_id === "session"));
	} finally {
		global.fetch = originalFetch;
	}
	const html = renderToStaticMarkup(
		React.createElement(RunControls, {
			busy: false,
			resumable: true,
			disabled: false,
			members: [
				{ session_id: "worker", name: "分析员", status: "awaiting_permission" },
			],
			confirmations: [confirmation],
			onResume() {},
			onCancel() {},
			onConfirm() {},
		}),
	);
	assert.match(html, /继续执行/);
	assert.match(html, /取消任务/);
	assert.match(html, /允许本次调用/);
	assert.match(html, /分析员：等待确认/);
	assert.match(html, /echo hello/);
});
