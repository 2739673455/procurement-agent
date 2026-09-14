(() => {
    const key = "procurement_assistant_panel";
    const launcherKey = "procurement_assistant_launcher";

    function close() {
        const panel = document.getElementById(key);
        if (panel) panel.remove();
        const launcher = document.getElementById(launcherKey);
        if (launcher) {
            launcher.hidden = false;
            launcher.setAttribute("aria-expanded", "false");
        }
    }

    const conversations = new Map();
    function show() {
        close();
        const route = frappe.get_route();
        const conversationKey = JSON.stringify([frappe.session.user, ...route]);
        if (!conversations.has(conversationKey)) {
            conversations.set(conversationKey, { turns: [], pending: false, error: "" });
        }
        const state = conversations.get(conversationKey);
        const panel = document.createElement("aside");
        panel.id = key;
        panel.className = "procurement-assistant-panel";
        panel.setAttribute("aria-label", "采购助手对话");
        const header = document.createElement("header");
        const title = document.createElement("h3");
        title.textContent = "采购助手";
        const closeButton = document.createElement("button");
        closeButton.className = "btn btn-default btn-sm";
        closeButton.textContent = "关闭";
        closeButton.onclick = () => { close(); document.getElementById(launcherKey)?.focus(); };
        header.append(title, closeButton);
        const context = document.createElement("p");
        context.className = "procurement-chat-context";
        context.textContent = `当前页面：${route.slice(1).join(" / ") || "Buying"}`;
        const log = document.createElement("div");
        log.className = "procurement-chat-log";
        log.setAttribute("role", "log");
        log.setAttribute("aria-live", "polite");
        const status = document.createElement("p");
        status.className = "procurement-chat-status";
        status.setAttribute("role", "status");
        const form = document.createElement("form");
        form.className = "procurement-chat-composer";
        const input = document.createElement("textarea");
        input.placeholder = "例如：查询名称包含螺丝的物料";
        input.setAttribute("aria-label", "发送给采购助手的问题");
        input.maxLength = 4000;
        input.rows = 3;
        const actions = document.createElement("div");
        const reset = document.createElement("button");
        reset.type = "button";
        reset.className = "btn btn-default btn-sm";
        reset.textContent = "清空对话";
        reset.onclick = () => { state.turns = []; state.error = ""; render(); };
        const send = document.createElement("button");
        send.type = "submit";
        send.className = "btn btn-primary btn-sm";
        send.textContent = "发送";
        actions.append(reset, send);
        form.append(input, actions);
        panel.append(header, context, log, status, form);

        function render() {
            log.replaceChildren();
            if (!state.turns.length) {
                const welcome = document.createElement("p");
                welcome.textContent = "你好，我可以查询 ERPNext 中你有权访问的 Item（物料）。可以按编码、名称搜索，也可以让我列出物料。当前只支持查询，不修改业务数据。";
                log.append(welcome);
            }
            for (const turn of state.turns) {
                const bubble = document.createElement("div");
                bubble.className = `procurement-chat-message ${turn.role}`;
                const label = document.createElement("strong");
                label.textContent = turn.role === "user" ? "你" : "采购助手";
                const content = document.createElement("p");
                content.textContent = turn.content;
                bubble.append(label, content);
                for (const query of turn.queries || []) {
                    const summary = document.createElement("p");
                    summary.textContent = `Item 查询「${query.query || "全部"}」：返回 ${query.items.length} 条，起始位置 ${query.offset}${query.has_more ? "，还有更多结果" : ""}`;
                    bubble.append(summary);
                    for (const item of query.items) {
                        if (!item.name) continue;
                        const link = document.createElement("button");
                        link.type = "button";
                        link.className = "btn btn-link btn-sm";
                        link.textContent = `${item.item_code || item.name} · ${item.item_name || "查看物料"}`;
                        link.onclick = () => frappe.set_route("Form", "Item", item.name);
                        bubble.append(link);
                    }
                }
                log.append(bubble);
            }
            status.textContent = state.pending ? "正在分析并查询物料…" : state.error;
            input.disabled = send.disabled = reset.disabled = state.pending;
            log.scrollTop = log.scrollHeight;
        }
        state.render = () => { if (panel.isConnected) render(); };
        form.onsubmit = async (event) => {
            event.preventDefault();
            const message = input.value.trim();
            if (!message || state.pending) return;
            // Only completed turns are sent; failed requests can be retried safely.
            const history = state.turns.filter(t => !t.failed).slice(-20).map(({role, content}) => ({role, content}));
            const turn = {role: "user", content: message};
            state.turns.push(turn);
            state.pending = true;
            state.error = "";
            input.value = "";
            render();
            try {
                const response = await frappe.call({
                    method: "procurement_assistant.api.chat",
                    args: {message, history: JSON.stringify(history)},
                });
                const result = response.message;
                if (result?.error) throw new Error(result.error);
                if (typeof result?.reply !== "string") throw new Error("服务未返回有效回复，请重试。");
                state.turns.push({role: "assistant", content: result.reply, queries: result.queries || []});
            } catch (error) {
                turn.failed = true;
                state.error = error instanceof Error ? error.message : "请求失败，请检查登录状态并重试。";
                if (panel.isConnected) input.value = message;
            } finally {
                state.pending = false;
                state.render();
            }
        };
        input.addEventListener("keydown", event => {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                event.preventDefault(); form.requestSubmit();
            }
        });
        panel.addEventListener("keydown", event => {
            if (event.key === "Escape") closeButton.click();
        });
        document.body.append(panel);
        document.getElementById(launcherKey)?.setAttribute("aria-expanded", "true");
        render();
        input.focus();
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
