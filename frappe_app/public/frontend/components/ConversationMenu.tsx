import { useEffect, useRef, useState } from "react";
import type { Conversation } from "../types";

interface Props {
    conversations: Conversation[];
    current: string | null;
    pending: boolean;
    onRefresh(): Promise<void>;
    onSelect(id: string): Promise<void>;
    onCreate(): Promise<void>;
    onDelete(id: string): Promise<void>;
}
export function ConversationMenu(props: Props) {
    const [open, setOpen] = useState(false);
    const picker = useRef<HTMLDivElement>(null);
    const trigger = useRef<HTMLButtonElement>(null);
    const title = props.conversations.find(row => row.id === props.current)?.title || "新会话";
    function close() { setOpen(false); trigger.current?.focus(); }
    useEffect(() => {
        if (!open) return;
        function outside(event: PointerEvent) {
            if (!picker.current?.contains(event.target as Node)) setOpen(false);
        }
        function escape(event: KeyboardEvent) {
            if (event.key === "Escape") { event.preventDefault(); close(); }
        }
        document.addEventListener("pointerdown", outside);
        document.addEventListener("keydown", escape);
        return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
    }, [open]);
    return <div ref={picker} className={`buying-ai-chat-picker${open ? " is-open" : ""}`}>
        <button ref={trigger} type="button" className="buying-ai-chat-title btn btn-default" title={title}
            aria-expanded={open} aria-controls="buying-ai-chat-history" onClick={() => {
                setOpen(!open); if (!open) void props.onRefresh();
            }}>
            <span>{title}</span><span className="buying-ai-chat-chevron" aria-hidden="true">
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6" /></svg>
            </span>
        </button>
        {open && <section id="buying-ai-chat-history" className="buying-ai-chat-history" aria-label="历史会话">
            {!props.conversations.length && <p>暂无历史会话</p>}
            {props.conversations.map(row => <div className="buying-ai-chat-history-row" key={row.id}>
                <button type="button" disabled={props.pending} className="btn btn-default btn-sm buying-ai-chat-history-title"
                    aria-current={row.id === props.current} onClick={() => { close(); void props.onSelect(row.id); }}>{row.title}</button>
                <button type="button" disabled={props.pending} className="btn btn-default btn-sm" aria-label={`删除${row.title}`}
                    onClick={() => void props.onDelete(row.id)}>删除</button>
            </div>)}
            <button type="button" disabled={props.pending} className="btn btn-default btn-sm buying-ai-chat-create"
                onClick={() => { close(); void props.onCreate(); }}>＋ 新建会话</button>
        </section>}
    </div>;
}
