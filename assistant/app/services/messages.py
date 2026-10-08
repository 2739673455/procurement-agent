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
    """提取工具结果中的文本，解析 JSON 或返回普通文本展示结构。"""
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
    """将框架消息转换为聊天记录，输出用户正文、回复文本和工具结果。"""
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
    """累积框架回复事件，将文本增量和工具结果转换为界面事件。"""

    def __init__(self, history: list[Msg]):
        """使用 history 保存累积的回复消息，并初始化当前回复。"""
        self.history = history
        self.reply: Msg | None = None

    def convert(self, event):
        """更新当前回复并产出界面事件，思考内容不对外展示。"""
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
