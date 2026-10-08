"""AgentScope 消息与前端聊天事件之间的投影；思考内容不对外展示。"""

import json

from agentscope.event import (
    ReplyStartEvent,
    TextBlockDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
)
from agentscope.message import (
    AssistantMsg,
    Msg,
    TextBlock,
    ToolResultBlock,
    ToolResultState,
)


def tool_result(block: ToolResultBlock):
    text = (
        block.output
        if isinstance(block.output, str)
        else "\n".join(
            item.text for item in block.output if isinstance(item, TextBlock)
        )
    )
    try:
        return json.loads(text)
    except ValueError:
        return {"content": text}


def public_messages(messages: list[Msg]):
    output = []
    for message in messages:
        if message.role == "user":
            metadata = message.metadata or {}
            output.append(
                {
                    "id": message.id,
                    "role": "user",
                    "content": metadata.get("display_text", message.get_text_content()),
                    "page_context": metadata.get("page_context"),
                    "attachments": metadata.get("attachments", []),
                }
            )
        elif message.role == "assistant":
            for block in message.content:
                if isinstance(block, TextBlock) and block.text:
                    output.append(
                        {"id": block.id, "role": "assistant", "content": block.text}
                    )
                elif (
                    isinstance(block, ToolResultBlock)
                    and block.state != ToolResultState.RUNNING
                ):
                    output.append(
                        {
                            "id": block.id,
                            "role": "tool",
                            "content": block.name,
                            "result": tool_result(block),
                        }
                    )
    return output


class StreamProjection:
    def __init__(self, history: list[Msg]):
        self.history = history
        self.reply: Msg | None = None

    def convert(self, event):
        if isinstance(event, ReplyStartEvent):
            self.reply = AssistantMsg(name=event.name, id=event.reply_id, content=[])
            self.history.append(self.reply)
        if self.reply is None:
            return
        self.reply.append_event(event)
        if isinstance(event, TextBlockDeltaEvent):
            yield {"type": "delta", "delta": event.delta, "message_id": event.block_id}
        elif isinstance(event, ToolResultStartEvent):
            yield {
                "type": "tool_start",
                "name": event.tool_call_name,
                "id": event.tool_call_id,
            }
        elif isinstance(event, ToolResultEndEvent):
            block = next(
                block
                for block in self.reply.content
                if isinstance(block, ToolResultBlock) and block.id == event.tool_call_id
            )
            yield {
                "type": "tool_result",
                "id": block.id,
                "name": block.name,
                "result": tool_result(block),
            }
