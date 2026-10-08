import { useRef, useState } from "react";
import type { Attachment } from "../types";
/** 输入区状态和发送、停止、上传、移除附件的回调。 */
interface Props {
    busy: boolean;
    disabled: boolean;
    uploading: boolean;
    attachments: Attachment[];
    /** 发送正文及页面携带选项，返回本轮是否成功完成。 */
    onSend(text: string, context: boolean): Promise<boolean>;
    /** 停止当前运行。 */
    onStop(): Promise<void>;
    /** 上传选择的文件并加入待发送附件列表。 */
    onUpload(files: File[]): Promise<void>;
    /** 按文件记录 ID 从待发送列表移除附件。 */
    onRemove(name: string): void;
}
/** 管理问题草稿、页面携带选项和附件，发送未成功时恢复草稿。 */
export function Composer(props: Props) {
    const [text, setText] = useState("");
    const [includeContext, setIncludeContext] = useState(true);
    const fileInput = useRef<HTMLInputElement>(null);
    const disabled = props.disabled || props.busy || props.uploading;
    /** 提交有效草稿，并按发送结果决定是否恢复输入内容。 */
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
