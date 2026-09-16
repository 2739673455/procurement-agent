import { useConversation } from "../useConversation";
import { Composer } from "./Composer";
import { ConversationMenu } from "./ConversationMenu";
import { MessageList } from "./MessageList";

export function AssistantPanel({ onClose }: { onClose(): void }) {
    const chat = useConversation();
    return <aside id="buying_ai_panel" className="buying-ai-panel" aria-label="采购助手对话">
        <header>
            <ConversationMenu conversations={chat.conversations} current={chat.current} pending={chat.pending}
                onRefresh={chat.refresh} onSelect={chat.select} onCreate={chat.create} onDelete={chat.remove} />
            <button type="button" className="btn btn-default btn-sm" onClick={onClose}>关闭</button>
        </header>
        <div className="buying-ai-chat-errors">{chat.error && <div className="buying-ai-chat-error" role="alert">
            <p>{chat.error}</p><button type="button" className="btn btn-default btn-sm" disabled={chat.loading}
                onClick={() => void chat.retry()}>重试</button>
        </div>}</div>
        <MessageList messages={chat.messages} loading={chat.loading} error={chat.error} />
        <p className="buying-ai-chat-status" role="status">{chat.status}</p>
        <Composer key={chat.current || "empty"} busy={chat.busy} disabled={!chat.current || chat.loading} uploading={chat.uploading}
            attachments={chat.attachments} onSend={chat.send} onStop={chat.stop} onUpload={chat.uploadFiles} onRemove={chat.removeAttachment} />
    </aside>;
}
