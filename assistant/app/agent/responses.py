"""从 dataagent 迁移的 Responses 适配器，支持回传 DeepSeek 工具调用轮次的历史消息。"""

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import Any
from uuid import uuid4

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import LangSmithParams, LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models import base as openai_chat_base


def _convert_deepseek_responses_chunk(
    chunk: Any,
    current_index: int,
    current_output_index: int,
    current_sub_index: int,
    *,
    schema: Any,
    metadata: dict[str, Any],
    has_reasoning: bool,
    output_version: str | None,
) -> tuple[int, int, int, ChatGenerationChunk | None]:
    """补充 LangChain 尚未处理的 DeepSeek 明文思考增量。"""
    if chunk.type != "response.reasoning_text.delta":
        return openai_chat_base._convert_responses_chunk_to_generation_chunk(  # pyright: ignore[reportPrivateUsage]
            chunk,
            current_index,
            current_output_index,
            current_sub_index,
            schema=schema,
            metadata=metadata,
            has_reasoning=has_reasoning,
            output_version=output_version,
        )

    if current_output_index != chunk.output_index:
        current_index += 1
    current_output_index = chunk.output_index
    current_sub_index = chunk.content_index
    return (
        current_index,
        current_output_index,
        current_sub_index,
        ChatGenerationChunk(
            message=AIMessageChunk(
                content=[
                    {
                        "type": "reasoning",
                        "content": [
                            {
                                "type": "reasoning_text",
                                "text": chunk.delta,
                                "index": chunk.content_index,
                            }
                        ],
                        "index": current_index,
                    }
                ]
            )
        ),
    )


class ResponsesModel(ChatOpenAI):
    """为每次 Responses API 调用分配稳定的公开消息 ID。"""

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        """统一同一次同步流式调用的消息块 ID。"""
        message_id = str(uuid4())
        for chunk in self._stream_responses(*args, **kwargs):
            chunk.message.id = message_id
            yield chunk

    async def _astream(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """统一同一次异步流式调用的消息块 ID。"""
        message_id = str(uuid4())
        async for chunk in self._astream_responses(*args, **kwargs):
            chunk.message.id = message_id
            yield chunk


class DeepSeekResponsesModel(ResponsesModel):
    """适配 DeepSeek 无状态 Responses 思考模式的后续轮次。"""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: dict | str | bool | None = None,
        strict: bool | None = None,
        parallel_tool_calls: bool | None = None,
        response_format: Any = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """绑定工具时，不发送 DeepSeek 思考模式不支持的 tool_choice 参数。"""
        del tool_choice
        return super().bind_tools(
            tools,
            strict=strict,
            parallel_tool_calls=parallel_tool_calls,
            response_format=response_format,
            **kwargs,
        )

    def _get_ls_params(
        self,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> LangSmithParams:
        """将 OpenAI 协议客户端的调用标记为实际使用的 DeepSeek 服务商。"""
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = "deepseek"
        return params

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """保留 DeepSeek 下一次工具调用必须回传的明文思考内容。"""
        # LangChain 会在 store=false 时移除 OpenAI 无法无状态重放的明文
        # 思考内容。DeepSeek 本身始终无状态，并要求客户端完整回传这些内容，
        # 因此先按完整历史序列化，再把发往模型服务的 store 参数恢复为 false。
        kwargs["store"] = True
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload["store"] = False
        return payload

    def _stream_responses(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """转换 DeepSeek Responses 同步流，包含明文思考增量。"""
        self._ensure_sync_client_available()
        kwargs["stream"] = True
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        context_manager = self.root_client.responses.create(**payload)
        original_schema = kwargs.get("response_format")

        with context_manager as response:
            state = (-1, -1, -1)
            has_reasoning = False
            for provider_chunk in response:
                *state_values, generation_chunk = _convert_deepseek_responses_chunk(
                    provider_chunk,
                    *state,
                    schema=original_schema,
                    metadata={},
                    has_reasoning=has_reasoning,
                    output_version=self.output_version,
                )
                state = tuple(state_values)
                if generation_chunk is None:
                    continue
                if run_manager is not None:
                    run_manager.on_llm_new_token(
                        generation_chunk.text,
                        chunk=generation_chunk,
                    )
                yield generation_chunk

    async def _astream_responses(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """转换 DeepSeek Responses 异步流，包含明文思考增量。"""
        kwargs["stream"] = True
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        context_manager = await self.root_async_client.responses.create(**payload)
        original_schema = kwargs.get("response_format")

        async with context_manager as response:
            state = (-1, -1, -1)
            has_reasoning = False
            async for provider_chunk in response:
                *state_values, generation_chunk = _convert_deepseek_responses_chunk(
                    provider_chunk,
                    *state,
                    schema=original_schema,
                    metadata={},
                    has_reasoning=has_reasoning,
                    output_version=self.output_version,
                )
                state = tuple(state_values)
                if generation_chunk is None:
                    continue
                if run_manager is not None:
                    await run_manager.on_llm_new_token(
                        generation_chunk.text,
                        chunk=generation_chunk,
                    )
                yield generation_chunk
