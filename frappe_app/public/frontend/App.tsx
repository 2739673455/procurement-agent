import { useEffect, useState } from "react";
import { frappe } from "./frappe";
import { AssistantPanel } from "./components/AssistantPanel";

/** Frappe 路由只控制挂载范围；面板关闭时由 React 清理请求和事件监听。 */
export function App() {
    const [inBuying, setInBuying] = useState(false);
    const [open, setOpen] = useState(false);
    useEffect(() => {
        let timer: ReturnType<typeof setTimeout>;
        /** 等待侧栏更新后同步 Buying 页面状态，并在离开时关闭面板。 */
        function sync() {
            clearTimeout(timer);
            // sidebar_title 在 Frappe 派发事件之后才更新。
            timer = setTimeout(() => {
                const buying = frappe.app?.sidebar?.sidebar_title?.toLowerCase() === "buying";
                setInBuying(buying);
                if (!buying) setOpen(false);
            }, 0);
        }
        /** 路由切换时关闭面板并重新判断挂载范围。 */
        function routeChanged() { setOpen(false); sync(); }
        const events = "sidebar_setup.buying_ai page-change.buying_ai form-refresh.buying_ai";
        frappe.router.on("change", routeChanged);
        window.$(document).on(events, sync);
        sync();
        return () => { clearTimeout(timer); frappe.router.off("change", routeChanged); window.$(document).off(events, sync); };
    }, []);
    if (!inBuying) return null;
    return <>
        {open && <AssistantPanel onClose={() => setOpen(false)} />}
        <button type="button" id="buying_ai_launcher" className="buying-ai-launcher btn btn-primary"
            title="打开采购助手" aria-label="打开采购助手" aria-controls="buying_ai_panel" aria-expanded={open} onClick={() => setOpen(!open)}>
            <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12 3v3M10 3h4M7 7h10a3 3 0 0 1 3 3v7a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3v-7a3 3 0 0 1 3-3Z" />
                <path d="M1 12v3M23 12v3M9 16h6" /><circle cx="8.5" cy="11.5" r=".8" fill="currentColor" /><circle cx="15.5" cy="11.5" r=".8" fill="currentColor" />
            </svg>
        </button>
    </>;
}
