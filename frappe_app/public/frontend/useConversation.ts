import { useEffect, useRef, useState } from "react";
import { api, events, upload, type Turn } from "./api";
import { frappe, pageSnapshot } from "./frappe";
import type { Attachment, Conversation, Message, PageContext } from "./types";

/** 管理会话状态；关闭或切换时中止订阅，不停止服务端正在执行的任务。 */
export function useConversation() {
    const storageKey = `buying-ai-conversation:${frappe.session.user}`;
    const [conversations, setConversations] = useState<Conversation[]>([]);
    const [current, setCurrent] = useState<string | null>(null);
    const [messages, setMessages] = useState<Message[]>([]);
    const [attachments, setAttachments] = useState<Attachment[]>([]);
    const [busy, setBusy] = useState(false);
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

    function report(cause: unknown, signal: AbortSignal) {
        if (!signal.aborted) setError(cause instanceof Error ? cause.message : "请求失败。");
    }
    function remember(id: string | null) {
        currentRef.current = id;
        setCurrent(id);
        if (id) localStorage.setItem(storageKey, id);
        else localStorage.removeItem(storageKey);
    }
    async function list(signal: AbortSignal) {
        const version = ++listVersion.current;
        const data = await api.list(signal);
        if (!signal.aborted && version === listVersion.current) setConversations(data.conversations);
        return data.conversations;
    }
    async function stream(id: string, signal: AbortSignal, turn?: Turn) {
        let done = false;
        let failure = "";
        let stopped = false;
        let answerId: string | undefined;
        try {
            await events(id, turn, signal, event => {
                if (signal.aborted) return;
                switch (event.type) {
                    case "delta": {
                        answerId = event.message_id || answerId || crypto.randomUUID();
                        const key = answerId;
                        setMessages(previous => previous.some(message => message.id === key)
                            ? previous.map(message => message.id === key ? { ...message, content: message.content + event.delta } : message)
                            : [...previous, { id: key, role: "assistant", content: event.delta }]);
                        break;
                    }
                    case "tool_start":
                        answerId = undefined;
                        setStatus(`正在执行工具：${event.name}…`);
                        break;
                    case "tool_result":
                        setMessages(previous => [...previous, { id: event.id, role: "tool", content: event.name, result: event.result }]);
                        break;
                    case "error": failure = event.error; break;
                    case "stopped": stopped = true; break;
                    case "done": done = true; break;
                }
            });
            if (!done) failure ||= "连接中断，请重试以恢复状态。";
        } catch (cause) {
            if (!signal.aborted) failure = cause instanceof Error ? cause.message : "连接中断。";
        } finally {
            if (!signal.aborted) {
                sending.current = false;
                // 最终以服务端持久化消息为准，重试只恢复订阅，不重复发送。
                try {
                    const history = await api.history(id, signal);
                    if (!signal.aborted) {
                        setMessages(history.messages);
                        setBusy(history.running);
                        setStatus(stopped ? "已停止生成。" : "");
                        if (history.running) failure ||= "任务仍在执行，请重试以恢复连接。";
                    }
                } catch (cause) {
                    if (!signal.aborted) {
                        setBusy(false);
                        failure ||= cause instanceof Error ? cause.message : "加载会话失败。";
                    }
                }
                if (!signal.aborted) setError(failure);
            }
        }
        return !signal.aborted && done && !failure && !stopped;
    }
    async function load(id: string | null) {
        view.current.abort();
        const controller = new AbortController();
        view.current = controller;
        const { signal } = controller;
        sending.current = false;
        remember(id);
        setMessages([]);
        setAttachments([]);
        setUploading(false);
        setLoading(Boolean(id));
        setBusy(false);
        setError("");
        setStatus("");
        if (!id) return;
        try {
            const history = await api.history(id, signal);
            if (signal.aborted) return;
            setMessages(history.messages);
            setBusy(history.running);
            setLoading(false);
            if (history.running) await stream(id, signal);
        } catch (cause) { report(cause, signal); }
        finally { if (!signal.aborted) setLoading(false); }
    }
    async function refresh() {
        const signal = lifetime.current.signal;
        try { await list(signal); }
        catch (cause) { report(cause, signal); }
    }
    async function retry() {
        const signal = view.current.signal;
        setLoading(true);
        try {
            const rows = await list(signal);
            if (signal.aborted) return;
            const preferred = currentRef.current || localStorage.getItem(storageKey);
            const id = rows.find(row => row.id === preferred)?.id || rows[0]?.id || null;
            // 同一会话重连时保留未发送附件。
            const pendingAttachments = attachments;
            const same = id === currentRef.current;
            const resumed = load(id);
            if (same) setAttachments(pendingAttachments);
            await resumed;
        } catch (cause) { report(cause, signal); }
        finally { if (!signal.aborted) setLoading(false); }
    }
    async function mutate(action: (signal: AbortSignal) => Promise<void>) {
        if (mutation.current) return;
        mutation.current = true;
        setPending(true);
        const signal = lifetime.current.signal;
        try { await action(signal); }
        catch (cause) { report(cause, signal); }
        finally { mutation.current = false; if (!signal.aborted) setPending(false); }
    }
    async function create() {
        await mutate(async signal => {
            const row = await api.create(signal);
            if (signal.aborted) return;
            void load(row.id);
            await list(signal);
        });
    }
    async function remove(id: string) {
        if (!window.confirm("删除此会话及其历史？正在执行的任务也会停止。")) return;
        await mutate(async signal => {
            await api.remove(id, signal);
            if (signal.aborted) return;
            const wasCurrent = currentRef.current === id;
            if (wasCurrent) void load(null);
            const rows = await list(signal);
            if (!signal.aborted && wasCurrent) void load(rows[0]?.id || null);
        });
    }
    async function send(text: string, includeContext: boolean) {
        const id = currentRef.current;
        if (!id || busy || loading || uploading || sending.current || !text.trim()) return false;
        const signal = view.current.signal;
        let context: PageContext | null;
        try { context = includeContext ? pageSnapshot() : null; }
        catch (cause) { report(cause, signal); return false; }
        sending.current = true;
        setError("");
        setBusy(true);
        setStatus("正在生成…");
        setMessages(previous => [...previous, { id: crypto.randomUUID(), role: "user", content: text.trim(),
            page_context: context, attachments: attachments.map(file => ({ name: file.file_name })) }]);
        const success = await stream(id, signal, { message: text.trim(), page_context: context, attachments: attachments.map(file => file.name) });
        if (success) setAttachments([]);
        return success;
    }
    async function stop() {
        const signal = view.current.signal;
        if (!currentRef.current) return;
        try { await api.stop(currentRef.current, signal); }
        catch (cause) { report(cause, signal); }
    }
    async function uploadFiles(files: File[]) {
        const signal = view.current.signal;
        setUploading(true);
        try {
            for (const file of files) {
                setStatus(`正在上传：${file.name}`);
                const attachment = await upload(file, signal);
                if (signal.aborted) return;
                setAttachments(previous => [...previous, attachment]);
            }
        } catch (cause) { report(cause, signal); }
        finally { if (!signal.aborted) { setUploading(false); setStatus(""); } }
    }
    useEffect(() => {
        lifetime.current = new AbortController();
        view.current = new AbortController();
        void retry();
        return () => { lifetime.current.abort(); view.current.abort(); };
        // 面板挂载时恢复一次；后续动作通过当前会话引用执行。
    }, []);
    return { conversations, current, messages, attachments, busy, loading, uploading, pending, status, error,
        select: load, refresh, retry, create, remove, send, stop, uploadFiles,
        removeAttachment: (name: string) => setAttachments(previous => previous.filter(file => file.name !== name)) };
}
