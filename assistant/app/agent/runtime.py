"""单个采购智能体，凭据仅保存在本次调用的上下文中。"""

import json
from dataclasses import dataclass

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_chunk_to_message,
)
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.runtime import Runtime

from app.agent.model import create_model
from app.agent.prompts import SYSTEM
from app.agent.tools.items import TOOL, query_items
from app.config import app_config
from app.errors import AgentError
from app.integrations.erpnext import ERPNext


@dataclass
class TurnContext:
    erp: ERPNext


def build_agent(checkpointer, model=None):
    model = (
        model if model is not None else create_model(app_config.cfg.lm_config.active)
    ).bind_tools([TOOL])

    async def respond(state: MessagesState):
        writer = get_stream_writer()
        full: AIMessageChunk | None = None
        async for chunk in model.astream([SystemMessage(SYSTEM), *state["messages"]]):
            if not isinstance(chunk, AIMessageChunk):
                raise AgentError("模型返回了无效的消息增量。", 502)
            full = chunk if full is None else full + chunk
            if chunk.text:
                writer({"type": "delta", "delta": chunk.text, "message_id": chunk.id})
        if (
            full is None
            or full.invalid_tool_calls
            or (not full.content and not full.tool_calls)
        ):
            raise AgentError("模型未返回回复。", 502)
        return {"messages": [message_chunk_to_message(full)]}

    async def execute_tools(state: MessagesState, runtime: Runtime[TurnContext]):
        writer = get_stream_writer()
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        results = []
        for call in last.tool_calls:
            writer({"type": "tool_start", "name": call["name"], "id": call["id"]})
            if call["name"] != "query_items":
                result = {"error": "仅支持 query_items 工具。"}
            else:
                result = await query_items(runtime.context.erp, call["args"])
            writer({"type": "tool_result", "id": call["id"], "result": result})
            results.append(
                ToolMessage(
                    content=json.dumps(result, ensure_ascii=False),
                    tool_call_id=call["id"],
                )
            )
        return {"messages": results}

    graph = StateGraph(MessagesState, context_schema=TurnContext)
    graph.add_node("model", respond)
    graph.add_node("tools", execute_tools)
    graph.add_edge(START, "model")
    graph.add_conditional_edges(
        "model",
        lambda state: (
            "tools" if getattr(state["messages"][-1], "tool_calls", []) else END
        ),
    )
    graph.add_edge("tools", "model")
    return graph.compile(checkpointer=checkpointer)


def turn_input(messages, text):
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
        HumanMessage(text),
    ]
