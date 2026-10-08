"""将框架消息总线上的原生事件投影到采购聊天界面，不持有运行状态。"""

import asyncio
from contextlib import suppress

from agentscope.app.message_bus import MessageBusKeys
from agentscope.event import AgentEvent, ReplyEndEvent
from pydantic import TypeAdapter

from app.services.messages import StreamProjection

EVENTS = TypeAdapter(AgentEvent)


async def session_events(runtime, user_id, identifier, task):
    if task is None:
        yield {"type": "done"}
        return
    bus = runtime.message_bus
    key = MessageBusKeys.session_events(identifier)
    ready = asyncio.Event()
    subscription = bus.subscribe(key, on_ready=ready.set)
    pending = asyncio.create_task(anext(subscription))
    projection = StreamProjection([])
    seen = set()
    stopped = False
    failed = False

    def project(payload):
        nonlocal stopped, failed
        event = EVENTS.validate_python(
            {key: value for key, value in payload.items() if key != "_entry_id"}
        )
        if hasattr(event, "reply_id"):
            yield from projection.convert(event)
        if isinstance(event, ReplyEndEvent):
            if event.finished_reason == "interrupted":
                stopped = True
            elif event.error:
                failed = True
                yield {
                    "type": "error",
                    "error": "执行失败或超时，请检查模型配置后重试。",
                }
            elif event.finished_reason == "exceed_max_iters":
                failed = True
                yield {
                    "type": "error",
                    "error": "本轮工具调用已达到上限，请缩小问题范围后重试。",
                }

    try:
        await ready.wait()
        # 先订阅再回放，用总线 entry id 去重，避免回放和实时事件之间的空窗。
        for entry_id, payload in await bus.log_read(
            key, max_count=MessageBusKeys.SESSION_REPLAY_MAX_LEN
        ):
            seen.add(entry_id)
            for output in project(payload):
                yield output
        while True:
            done, _ = await asyncio.wait(
                {pending, task}, return_when=asyncio.FIRST_COMPLETED
            )
            if pending in done:
                try:
                    payload = pending.result()
                except StopAsyncIteration:
                    break
                entry_id = payload.get("_entry_id")
                if entry_id is None or entry_id not in seen:
                    for output in project(payload):
                        yield output
                pending = asyncio.create_task(anext(subscription))
                # 允许订阅消费已经入队的末尾事件，之后再判断运行是否结束。
                await asyncio.sleep(0)
            elif task in done:
                break
        # 任务可能在订阅建立前完成，框架此时已将回放日志落入消息表并清空。
        # 从持久消息补齐终态，避免快速失败或快速停止被展示为成功。
        if task.done():
            messages, _ = await runtime.storage.list_messages(
                user_id, identifier, limit=1
            )
            if messages and messages[-1].role == "assistant":
                last = messages[-1]
                stopped = stopped or last.finished_reason == "interrupted"
                if not failed and (
                    last.error or last.finished_reason == "exceed_max_iters"
                ):
                    failed = True
                    yield {"type": "error", "error": "本轮回复未能完成，请稍后重试。"}
        if task.cancelled() or stopped:
            yield {"type": "stopped"}
        elif task.done() and task.exception() is not None:
            yield {"type": "error", "error": "对话执行失败，请稍后重试。"}
        yield {"type": "done"}
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError, StopAsyncIteration):
            await pending
        await subscription.aclose()
