"""将持久化的图消息转换为对外聊天协议。"""

import json

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage


def public_messages(messages):
    output = []
    for message in messages:
        if isinstance(message, (HumanMessage, AIMessage)) and message.text:
            output.append(
                {
                    "id": message.id,
                    "role": "user"
                    if isinstance(message, HumanMessage)
                    else "assistant",
                    "content": message.additional_kwargs.get(
                        "display_text", message.text
                    ),
                    "page_context": message.additional_kwargs.get("page_context"),
                    "attachments": message.additional_kwargs.get("attachments", []),
                }
            )
        elif isinstance(message, ToolMessage):
            try:
                result = json.loads(str(message.content))
            except ValueError:
                result = {"content": message.text}
            output.append(
                {
                    "id": message.id,
                    "role": "tool",
                    "content": message.name or "工具结果",
                    "result": result,
                }
            )
    return output


def turn_input(messages, incoming):
    """追加新一轮消息前，为取消后未完成的工具调用补齐中断结果。"""
    pending = {}
    for message in messages:
        if isinstance(message, AIMessage):
            pending.update({call["id"]: call for call in message.tool_calls})
        elif isinstance(message, ToolMessage):
            pending.pop(message.tool_call_id, None)
    return [
        *(
            ToolMessage(content="上轮执行已中断，未取得结果。", tool_call_id=key)
            for key in pending
        ),
        incoming,
    ]


class StreamProjection:
    """将主图原生消息和节点更新转换为聊天事件，每次执行独立去重。"""

    def __init__(self):
        self.started: set[str] = set()
        self.finished: set[str] = set()

    def convert(self, mode, event):
        if mode == "messages":
            chunk, metadata = event
            if (
                metadata.get("langgraph_node") == "model"
                and isinstance(chunk, AIMessageChunk)
                and chunk.text
            ):
                yield {"type": "delta", "delta": chunk.text, "message_id": chunk.id}
            return
        if mode != "updates" or not isinstance(event, dict):
            return
        for update in event.values():
            if not isinstance(update, dict):
                continue
            messages = update.get("messages", [])
            if isinstance(messages, (AIMessage, ToolMessage)):
                messages = [messages]
            for message in messages:
                if isinstance(message, AIMessage):
                    for call in message.tool_calls:
                        if call["id"] and call["id"] not in self.started:
                            self.started.add(call["id"])
                            yield {
                                "type": "tool_start",
                                "name": call["name"],
                                "id": call["id"],
                            }
                elif (
                    isinstance(message, ToolMessage)
                    and message.tool_call_id not in self.finished
                ):
                    self.finished.add(message.tool_call_id)
                    projected = public_messages([message])[0]
                    yield {
                        "type": "tool_result",
                        "id": message.tool_call_id,
                        "name": message.name,
                        "result": projected["result"],
                    }
