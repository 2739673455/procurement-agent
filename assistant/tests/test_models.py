import asyncio
import json
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
from agentscope.message import (
    AssistantMsg,
    Msg,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.model import ChatModelBase, ChatResponse, DeepSeekChatModel
from openai import AsyncOpenAI
from pydantic import ValidationError

from app.config.agents import AgentDefinitions, ModelConfig, cfg
from app.runtime.models import ChatCredential

type ModelFactory = Callable[[dict[str, Any] | None], DeepSeekChatModel]


@pytest.fixture
def make_model(monkeypatch: pytest.MonkeyPatch) -> ModelFactory:
    monkeypatch.setattr(cfg, "models", dict(cfg.models))

    def build(params: dict[str, Any] | None = None) -> DeepSeekChatModel:
        cfg.models["protocol-test"] = ModelConfig.model_validate(
            {
                "credential": {
                    "type": "deepseek_credential",
                    "base_url": "https://model.invalid",
                    "api_key": "test",
                },
                "model": "test",
                "client_kwargs": {"timeout": 30, "max_retries": 0},
                "params": params or {},
                "image_inputs": True,
                "context_size": 32768,
            }
        )
        credential = ChatCredential(
            id="test-credential", name="test", config_key="protocol-test"
        )
        return cast(
            DeepSeekChatModel,
            credential.get_chat_model_class()(credential=credential, model="test"),
        )

    return build


@pytest.mark.parametrize(
    "overrides",
    [
        {"credential": {"type": "unknown_credential", "api_key": "test"}},
        {"params": {"max_tokens": -1}},
        {"params": {"reasoning_effort": "low"}},
    ],
)
def test_invalid_provider_configuration_rejected(overrides: dict[str, Any]) -> None:
    config = {
        "credential": {"type": "deepseek_credential", "api_key": "test"},
        "model": "test",
        "client_kwargs": {},
        "params": {},
        "image_inputs": False,
        "context_size": None,
    }
    with pytest.raises(ValidationError):
        ModelConfig.model_validate({**config, **overrides})


@pytest.mark.parametrize("reference", [None, "missing-model"])
def test_agent_requires_declared_model(reference: str | None) -> None:
    """Agent 必须显式引用 models 中已声明的模型，不能使用空值或未知配置名。"""
    config = cfg.model_dump()
    config["agents"][cfg.default]["model"] = reference
    with pytest.raises(ValidationError, match="model|未知模型"):
        AgentDefinitions.model_validate(config)


def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": "chat-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def stream_response(chunks: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
        + "data: [DONE]\n\n",
    )


async def final(model: ChatModelBase, messages: list[Msg]) -> ChatResponse:
    result = await model(messages)
    assert not isinstance(result, ChatResponse)
    chunks = [chunk async for chunk in result]
    return chunks[-1]


def test_deepseek_reasoning_and_tool_history_replay(make_model: ModelFactory) -> None:
    async def scenario() -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
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
            {"thinking_enable": True, "reasoning_effort": "high", "max_tokens": 128}
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
            assert payload["max_tokens"] == 128
            assert "max_completion_tokens" not in payload
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
