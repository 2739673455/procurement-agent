import asyncio
import json

import httpx
import pytest
from agentscope.message import (
    AssistantMsg,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from openai import AsyncOpenAI
from pydantic import SecretStr

from app.config import app_config
from app.config.app_config import ModelConfig
from app.runtime.models import ChatCompletionsModel, ChatCredential


@pytest.fixture
def make_model(monkeypatch):
    monkeypatch.setattr(
        app_config.cfg.lm_config, "models", dict(app_config.cfg.lm_config.models)
    )

    def build(params=None):
        app_config.cfg.lm_config.models["protocol-test"] = ModelConfig(
            model_provider="deepseek",
            model="test",
            base_url="https://model.invalid",
            api_key=SecretStr("test"),
            params=params or {},
            image_inputs=True,
            context_size=32768,
            timeout_seconds=30,
        )
        return ChatCompletionsModel(
            credential=ChatCredential(
                id="test-credential", name="test", config_key="protocol-test"
            ),
            model="test",
        )

    return build


def chunk(delta, finish_reason=None):
    return {
        "id": "chat-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def stream_response(chunks):
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
        + "data: [DONE]\n\n",
    )


async def final(model, messages):
    result = await model(messages)
    chunks = [chunk async for chunk in result]
    return chunks[-1]


def test_deepseek_reasoning_and_tool_history_replay(make_model):
    async def scenario():
        requests = []

        def handler(request):
            assert request.url.path == "/chat/completions"
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return stream_response(
                    [
                        chunk({"role": "assistant", "reasoning_content": "先查"}),
                        chunk({"reasoning_content": "物料"}),
                        chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "query_items",
                                            "arguments": "{",
                                        },
                                    }
                                ]
                            }
                        ),
                        chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": "}"},
                                    }
                                ]
                            }
                        ),
                        chunk({}, "tool_calls"),
                    ]
                )
            return stream_response(
                [
                    chunk({"reasoning_content": "已查到结果"}),
                    chunk({"content": "找到物料"}),
                    chunk({}, "cancel"),
                ]
            )

        model = make_model(
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
        )
        await model.client.close()
        model.client = AsyncOpenAI(
            api_key="test",
            base_url="https://model.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            user = UserMsg(name="user", content="查询")
            result = await final(model, [user])
            call = next(b for b in result.content if isinstance(b, ToolCallBlock))
            assert call.id == "call_1" and call.input == "{}"
            thinking = next(b for b in result.content if isinstance(b, ThinkingBlock))
            assert thinking.thinking == "先查物料"
            history = AssistantMsg(
                name="assistant",
                content=[
                    *result.content,
                    ToolResultBlock(
                        id=call.id,
                        name=call.name,
                        output="{}",
                        state=ToolResultState.SUCCESS,
                    ),
                ],
            )
            answer = await final(model, [user, history])
            history.content.extend(answer.content)
            await final(model, [user, history, UserMsg(name="user", content="继续")])
            payload = requests[-1]
            assert payload["thinking"] == {"type": "enabled"}
            assert payload["reasoning_effort"] == "high"
            assert payload["stream"] is True
            assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
            assert [m["reasoning_content"] for m in assistants] == [
                "先查物料",
                "已查到结果",
            ]
            assert assistants[0]["tool_calls"][0]["id"] == "call_1"
            tool = next(m for m in payload["messages"] if m["role"] == "tool")
            assert tool["tool_call_id"] == "call_1" and tool["content"] == "{}"
            assert assistants[-1]["content"] == "找到物料"
        finally:
            await model.client.close()

    asyncio.run(scenario())
