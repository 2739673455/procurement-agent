import { useRef, useState } from "react";
import type { Attachment } from "../types";
interface Props {
    busy: boolean;
    disabled: boolean;
    uploading: boolean;
    attachments: Attachment[];
    onSend(text: string, context: boolean): Promise<boolean>;
    onStop(): Promise<void>;
    onUpload(files: File[]): Promise<void>;
    onRemove(name: string): void;
}
export function Composer(props: Props) {
    const [text, setText] = useState("");
    const [includeContext, setIncludeContext] = useState(true);
    const fileInput = useRef<HTMLInputElement>(null);
    const disabled = props.disabled || props.busy || props.uploading;
    function submit() {
        if (disabled || !text.trim()) return;
        const draft = text;
        setText("");
        void props.onSend(draft, includeContext).then(success => {
            // 未完成的发送保留草稿，恢复连接不会自动重发。
            if (!success) setText(current => current || draft);
        });
    }
    return <form className="buying-ai-chat-composer" onSubmit={event => { event.preventDefault(); submit(); }}>
        <textarea className="form-control" rows={3} aria-label="发送给采购助手的问题" value={text} disabled={disabled}
            onChange={event => setText(event.target.value)} onKeyDown={event => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); submit(); }
            }} />
        <div className="buying-ai-chat-attachments">{props.attachments.map(file => <button type="button" key={file.name}
            className="btn btn-default btn-sm" disabled={props.busy || props.uploading} onClick={() => props.onRemove(file.name)}>{file.file_name} ×</button>)}</div>
        <input type="file" multiple hidden ref={fileInput} onChange={event => {
            const files = Array.from(event.target.files || []); event.target.value = "";
            if (files.length) void props.onUpload(files);
        }} />
        <div className="buying-ai-chat-actions">
            <button type="button" className="btn btn-default btn-sm" disabled={disabled} onClick={() => fileInput.current?.click()}>上传附件</button>
            <label className="buying-ai-chat-context-toggle" title="随消息发送当前页面及未保存的表单内容">
                <input type="checkbox" checked={includeContext} onChange={event => setIncludeContext(event.target.checked)} /> 携带页面内容
            </label>
            <button type={props.busy ? "button" : "submit"} className="btn btn-primary btn-sm buying-ai-chat-send"
                disabled={!props.busy && (disabled || !text.trim())} onClick={props.busy ? () => void props.onStop() : undefined}>{props.busy ? "停止" : "发送"}</button>
        </div>
    </form>;
}
