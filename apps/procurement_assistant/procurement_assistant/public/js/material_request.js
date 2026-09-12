(() => {
    const key = "procurement_assistant_panel";

    function close() {
        const panel = document.getElementById(key);
        if (panel) panel.remove();
    }

    function show(frm) {
        close();
        const panel = document.createElement("aside");
        panel.id = key;
        panel.className = "procurement-assistant-panel";
        panel.setAttribute("aria-label", "采购助手");
        const header = document.createElement("header");
        const title = document.createElement("h3");
        title.textContent = "采购助手";
        const button = document.createElement("button");
        button.className = "btn btn-default btn-sm";
        button.textContent = "关闭";
        button.onclick = () => { close(); frm.page.wrapper.find("button").filter((_, el) => el.textContent.trim() === "采购助手").trigger("focus"); };
        header.append(title, button);
        panel.append(header);
        function paragraph(text) {
            const p = document.createElement("p");
            p.textContent = text;
            panel.append(p);
        }
        paragraph(`当前单据：${frm.is_new() ? "未保存" : frm.doc.name}`);
        paragraph(`公司：${frm.doc.company || "未填写"}`);
        if (frm.is_dirty() || frm.is_new()) {
            paragraph("请先保存需求，再打开助手查看正式单据内容。");
        } else {
            const selected = frm.get_selected().items || [];
            const items = (frm.doc.items || []).filter(row => !selected.length || selected.includes(row.name));
            paragraph(selected.length ? `范围：已选 ${items.length} 项` : `范围：全部 ${items.length} 项`);
            const list = document.createElement("ul");
            for (const row of items) {
                const item = document.createElement("li");
                item.textContent = `${row.item_code || row.item_name || "未填写物料"} · ${row.qty ?? "—"} ${row.uom || ""} · 需求日期：${row.schedule_date || "未填写"}`;
                list.append(item);
            }
            panel.append(list);
        }
        paragraph("入口原型已接入 ERPNext。供应商筛选和对话将在接入 Agent 后开放。");
        paragraph("修改单据或明细选择后，请重新打开面板刷新上下文。");
        panel.addEventListener("keydown", event => {
            if (event.key === "Escape") button.click();
        });
        document.body.append(panel);
        button.focus();
    }

    // Register once: Frappe's event wrapper does not preserve handler identity.
    if (!window.procurement_assistant_route_handler_registered) {
        frappe.router.on("change", () => document.getElementById(key)?.remove());
        window.procurement_assistant_route_handler_registered = true;
    }

    frappe.ui.form.on("Material Request", {
        refresh(frm) {
            close();
            if (frm.doc.material_request_type === "Purchase") {
                frm.add_custom_button(__("采购助手"), () => show(frm));
            }
        },
        material_request_type(frm) {
            close();
            frm.remove_custom_button(__("采购助手"));
            if (frm.doc.material_request_type === "Purchase") {
                frm.add_custom_button(__("采购助手"), () => show(frm));
            }
        },
    });
})();
