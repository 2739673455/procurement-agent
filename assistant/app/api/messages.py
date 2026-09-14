"""将持久化的图消息转换为对外聊天协议。"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


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
                    "content": message.text,
                }
            )
        elif isinstance(message, ToolMessage):
            try:
                result = json.loads(str(message.content))
            except ValueError:
                continue
            output.append(
                {
                    "id": message.id,
                    "role": "tool",
                    "content": "Item 查询结果",
                    "result": result,
                }
            )
    return output
