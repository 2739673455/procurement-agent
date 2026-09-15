(() => {
    const key = "procurement_assistant_panel";
    const launcherKey = "procurement_assistant_launcher";
    let connection;
    let generation = 0;

    function close() {
        generation++;
        connection?.abort();
        document.getElementById(key)?.remove();
        document.getElementById(launcherKey)?.setAttribute("aria-expanded", "false");
    }

    async function request(action, data = {}, signal) {
        const response = await fetch("/api/method/procurement_assistant.api.conversations", {
            method: "POST", credentials: "same-origin", signal,
            headers: {"Content-Type": "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token},
            body: JSON.stringify({action, ...data}),
        });
        if (response.headers.get("content-type")?.includes("text/event-stream")) return response;
        const body = await response.json();
        const result = body.message || body;
        if (!response.ok || result.error) throw new Error(result.error || "请求失败，请检查登录状态。");
        return result;
    }

    function pageSnapshot() {
        const route = frappe.get_route();
        const frm = window.cur_frm;
        if (route[0] !== "Form" || !frm?.doc || route[1] !== frm.doctype || route[2] !== frm.doc.name) return {route};
        function fields(doc) {
            const result = {};
            for (const field of frappe.get_meta(doc.doctype).fields) {
                if (field.fieldtype === "Password" || doc[field.fieldname] === undefined) continue;
                result[field.fieldname] = ["Table", "Table MultiSelect"].includes(field.fieldtype)
                    ? (doc[field.fieldname] || []).map(fields) : doc[field.fieldname];
            }
            return result;
        }
        return {route, doctype: frm.doctype, name: frm.doc.name,
            is_new: Boolean(frm.is_new()), is_dirty: Boolean(frm.is_dirty()), doc: fields(frm.doc)};
    }

    async function show() {
        close();
        const version = generation;
        const storageKey = `procurement-conversation:${frappe.session.user}`;
        let current = localStorage.getItem(storageKey);
        let busy = false;
        let uploading = false;
        let attachments = [];
        const panel = document.createElement("aside");
        panel.id = key;
        panel.className = "procurement-assistant-panel";
        panel.setAttribute("aria-label", "采购助手对话");
        const header = document.createElement("header");
        const title = document.createElement("h3");
        title.textContent = "采购助手";
        function button(label, handler) {
            const el = document.createElement("button");
            el.type = "button";
            el.className = "btn btn-default btn-sm";
            el.textContent = label;
            el.onclick = handler;
            return el;
        }
        header.append(title, button("关闭", close));
        const select = document.createElement("select");
        select.className = "form-control";
        select.setAttribute("aria-label", "选择对话");
        const toolbar = document.createElement("div");
        toolbar.className = "procurement-chat-toolbar";
        const context = document.createElement("p");
        context.className = "procurement-chat-context";
        const includeContext = document.createElement("input");
        includeContext.type = "checkbox";
        includeContext.checked = true;
        const contextLabel = document.createElement("label");
        contextLabel.append(includeContext, " 随消息发送当前页面和表单内容");
        function updateContext() {
            const snapshot = pageSnapshot();
            context.textContent = `当前页面：${snapshot.route.join(" / ")}${snapshot.doctype ? (snapshot.is_new ? "（新建，尚未保存）" : snapshot.is_dirty ? "（包含未保存修改）" : "（已保存）") : ""}`;
        }
        updateContext();
        const log = document.createElement("div");
        log.className = "procurement-chat-log";
        log.setAttribute("role", "log");
        const status = document.createElement("p");
        status.className = "procurement-chat-status";
        status.setAttribute("role", "status");
        const form = document.createElement("form");
        form.className = "procurement-chat-composer";
        const input = document.createElement("textarea");
        input.rows = 3;
        input.placeholder = "例如：查询名称包含螺丝的物料";
        input.setAttribute("aria-label", "发送给采购助手的问题");
        const actions = document.createElement("div");
        const send = button("发送", () => form.requestSubmit());
        const stop = button("停止生成", () => guard(async () => {
            await request("stop", {conversation_id: current});
        }));
        const fileInput = document.createElement("input");
        fileInput.type = "file";
        fileInput.multiple = true;
        fileInput.hidden = true;
        const attach = button("上传附件", () => fileInput.click());
        const attachmentList = document.createElement("div");
        attachmentList.className = "procurement-chat-attachments";
        const notice = document.createElement("small");
        notice.textContent = "附件保存为私有文件；发送后，所选附件和勾选的表单内容会交给模型处理。";
        actions.append(attach, stop, send);
        form.append(input, attachmentList, fileInput, actions, notice);
        panel.append(header, select, toolbar, contextLabel, context, log, status, form);
        function renderAttachments() {
            attachmentList.replaceChildren();
            for (const file of attachments) {
                attachmentList.append(button(`${file.file_name} ×`, () => {
                    attachments = attachments.filter(item => item.name !== file.name);
                    renderAttachments();
                }));
            }
        }
        fileInput.onchange = () => guard(async () => {
            uploading = true;
            setBusy(busy);
            const selected = [...fileInput.files];
            const selectedConversation = current;
            try {
                for (const file of selected) {
                    status.textContent = `正在上传：${file.name}`;
                    const body = new FormData();
                    body.append("file", file);
                    body.append("is_private", "1");
                    const response = await fetch("/api/method/upload_file", {
                        method: "POST", credentials: "same-origin",
                        headers: {"X-Frappe-CSRF-Token": frappe.csrf_token}, body,
                    });
                    const result = await response.json();
                    if (!response.ok || !result.message?.name) throw new Error(`附件上传失败：${file.name}`);
                    if (!active() || current !== selectedConversation) return;
                    attachments.push(result.message);
                    renderAttachments();
                }
            } finally {
                uploading = false;
                fileInput.value = "";
                if (active()) setBusy(busy);
            }
        });
        document.body.append(panel);
        document.getElementById(launcherKey)?.setAttribute("aria-expanded", "true");

        function active() { return generation === version && panel.isConnected; }
        async function guard(fn) {
            try { await fn(); }
            catch (error) { if (active() && error.name !== "AbortError") status.textContent = error.message; }
        }
        function setBusy(value) {
            busy = value;
            input.disabled = send.disabled = value || uploading || !current;
            attach.disabled = value || uploading || !current;
            stop.disabled = !value;
            status.textContent = value ? "正在生成…" : "";
        }
        function bubble(role, content) {
            const el = document.createElement("div");
            el.className = `procurement-chat-message ${role}`;
            const label = document.createElement("strong");
            label.textContent = role === "user" ? "你" : role === "tool" ? "工具结果" : "采购助手";
            const text = document.createElement("p");
            text.textContent = typeof content === "string" ? content : JSON.stringify(content);
            el.append(label, text);
            log.append(el);
            log.scrollTop = log.scrollHeight;
            return {el, text};
        }
        function renderMessage(message) {
            const {el} = bubble(message.role, message.content);
            const details = [];
            if (message.page_context) details.push(`页面：${message.page_context.route.join(" / ")}${message.page_context.is_new || message.page_context.is_dirty ? "（发送时未保存）" : ""}`);
            for (const attachment of message.attachments || []) details.push(`附件：${attachment.name}`);
            if (details.length) {
                const meta = document.createElement("small");
                meta.textContent = details.join("\n");
                meta.style.whiteSpace = "pre-wrap";
                el.append(meta);
            }
        }
        function toolResult(result) {
            if (!result || !Array.isArray(result.items)) {
                bubble("tool", typeof result === "string" ? result : result?.error || result?.content || JSON.stringify(result));
                return;
            }
            const {el} = bubble("tool", result.error || `返回 ${result.items?.length || 0} 条物料，起始位置 ${result.offset || 0}${result.has_more ? "，还有更多结果" : ""}`);
            for (const item of result.items || []) {
                el.append(button(`${item.item_code || item.name} · ${item.item_name || "查看物料"}`, () => frappe.set_route("Form", "Item", item.name)));
            }
        }
        async function list() {
            const data = await request("list");
            if (!active()) return;
            select.replaceChildren();
            for (const row of data.conversations) {
                const option = document.createElement("option");
                option.value = row.id;
                option.textContent = row.title;
                select.append(option);
            }
            if (!data.conversations.some(row => row.id === current)) current = data.conversations[0]?.id || null;
            if (current) {
                select.value = current;
                localStorage.setItem(storageKey, current);
            } else localStorage.removeItem(storageKey);
        }
        async function load() {
            connection?.abort();
            log.replaceChildren();
            if (!current) {
                bubble("assistant", "新建对话后，可以让我查询你有权访问的 ERPNext 物料。");
                setBusy(false);
                return;
            }
            const id = current;
            const data = await request("messages", {conversation_id: id});
            if (!active() || current !== id) return;
            for (const message of data.messages) {
                if (message.role === "tool") toolResult(message.result);
                else renderMessage(message);
            }
            setBusy(data.running);
            if (data.running) await stream("subscribe", id);
        }
        async function stream(action, id, message, extra = {}) {
            const controller = new AbortController();
            connection = controller;
            let errorText = "";
            let answer;
            let done = false;
            try {
                const response = await request(action, {conversation_id: id, ...(message ? {message} : {}), ...extra}, controller.signal);
                if (!(response instanceof Response)) throw new Error("未收到对话事件流。");
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = "";
                while (true) {
                    const chunk = await reader.read();
                    if (chunk.done) break;
                    buffer += decoder.decode(chunk.value, {stream: true});
                    let boundary;
                    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
                        const frame = buffer.slice(0, boundary);
                        buffer = buffer.slice(boundary + 2);
                        if (!frame.startsWith("data: ")) continue;
                        const event = JSON.parse(frame.slice(6));
                        if (!active() || current !== id) return;
                        if (event.type === "delta") {
                            answer ||= bubble("assistant", "");
                            answer.text.textContent += event.delta;
                        } else if (event.type === "tool_start") {
                            answer = null;
                            status.textContent = `正在执行工具：${event.name}…`;
                        } else if (event.type === "tool_result") toolResult(event.result);
                        else if (event.type === "error") errorText = event.error;
                        else if (event.type === "stopped") errorText = "已停止生成。";
                        else if (event.type === "done") done = true;
                        log.scrollTop = log.scrollHeight;
                    }
                }
                if (!done) throw new Error("连接中断，请重新打开对话恢复状态。");
            } finally {
                if (active() && current === id && !controller.signal.aborted) {
                    setBusy(false);
                    // Checkpoints are the source of truth; replace transient stream output.
                    const data = await request("messages", {conversation_id: id});
                    if (active() && current === id) {
                        log.replaceChildren();
                        for (const item of data.messages) {
                            if (item.role === "tool") toolResult(item.result);
                            else renderMessage(item);
                        }
                        setBusy(data.running);
                        status.textContent = errorText || (data.running ? "任务仍在执行，请重新打开对话连接。" : "");
                    }
                }
            }
            return !errorText;
        }
        toolbar.append(
            button("新建", () => guard(async () => {
                attachments = [];
                renderAttachments();
                const data = await request("create");
                current = data.id;
                await list(); await load();
            })),
            button("重命名", () => guard(async () => {
                if (!current) return;
                const title = window.prompt("对话名称", select.selectedOptions[0]?.textContent || "");
                if (!title?.trim()) return;
                await request("rename", {conversation_id: current, title: title.trim()});
                await list();
            })),
            button("删除", () => guard(async () => {
                if (!current || !window.confirm("删除此对话及其历史？正在执行的任务也会停止。")) return;
                await request("delete", {conversation_id: current});
                attachments = [];
                renderAttachments();
                current = null;
                await list(); await load();
            })),
        );
        select.onchange = () => guard(async () => {
            attachments = [];
            renderAttachments();
            current = select.value;
            localStorage.setItem(storageKey, current);
            await load();
        });
        form.onsubmit = event => {
            event.preventDefault();
            const message = input.value.trim();
            if (!message || busy || uploading || !current) return;
            updateContext();
            const page_context = includeContext.checked ? pageSnapshot() : null;
            const selectedAttachments = attachments;
            input.value = "";
            renderMessage({role: "user", content: message, page_context,
                attachments: selectedAttachments.map(file => ({name: file.file_name}))});
            setBusy(true);
            guard(async () => {
                const sent = await stream("send", current, message, {page_context, attachments: selectedAttachments.map(file => file.name)});
                if (sent && active() && attachments === selectedAttachments) {
                    attachments = [];
                    renderAttachments();
                }
            });
        };
        input.onfocus = updateContext;
        input.onkeydown = event => {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                event.preventDefault(); form.requestSubmit();
            }
        };
        setBusy(false);
        await guard(async () => { await list(); await load(); });
    }

    function syncLauncher() {
        const inBuying = frappe.app?.sidebar?.sidebar_title?.toLowerCase() === "buying";
        if (!inBuying) {
            close();
            document.getElementById(launcherKey)?.remove();
            return;
        }
        if (document.getElementById(launcherKey)) return;

        const launcher = document.createElement("button");
        launcher.id = launcherKey;
        launcher.type = "button";
        launcher.className = "procurement-assistant-launcher";
        launcher.title = "打开采购助手";
        launcher.setAttribute("aria-label", "打开采购助手");
        launcher.setAttribute("aria-controls", key);
        launcher.setAttribute("aria-expanded", "false");
        // Static icon markup only; business data is rendered with textContent.
        launcher.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v3M10 3h4M7 7h10a3 3 0 0 1 3 3v7a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3v-7a3 3 0 0 1 3-3Z"/><path d="M1 12v3M23 12v3M9 16h6"/><circle cx="8.5" cy="11.5" r=".8" fill="currentColor"/><circle cx="15.5" cy="11.5" r=".8" fill="currentColor"/></svg>';
        launcher.onclick = () => {
            if (document.getElementById(key)) close();
            else show();
        };
        document.body.append(launcher);
    }

    // Sidebar setup updates sidebar_title after emitting its event.
    let scheduled;
    function scheduleSync() {
        clearTimeout(scheduled);
        scheduled = setTimeout(syncLauncher, 0);
    }
    frappe.router.on("change", () => {
        close();
        scheduleSync();
    });
    $(document).on("sidebar_setup.procurement_assistant page-change.procurement_assistant form-refresh.procurement_assistant", scheduleSync);
    scheduleSync();
})();
