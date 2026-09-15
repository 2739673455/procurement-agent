"""参考 dataagent 实现的会话运行管理，任务生命周期独立于客户端连接。"""

import asyncio
from dataclasses import dataclass, field

from langchain_core.runnables import RunnableConfig
from loguru import logger

from app.agent.context import TurnContext
from app.agent.runtime import build_agent
from app.errors.agent import AgentError
from app.observability import context
from app.services.inputs import user_message
from app.services.messages import StreamProjection, turn_input


@dataclass
class Run:
    subscribers: set = field(default_factory=set)
    task: asyncio.Task | None = None

    def publish(self, event):
        for queue in tuple(self.subscribers):
            if queue.full():
                self.subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(
                    {"type": "error", "error": "连接过慢，请重新打开对话恢复历史。"}
                )
                queue.put_nowait({"type": "done"})
            else:
                queue.put_nowait(event)


class AgentRunService:
    def __init__(self, checkpointer, repository):
        self.checkpointer = checkpointer
        self.repository = repository
        self.agent = None
        self.runs: dict[str, Run] = {}

    @staticmethod
    def config(identifier) -> RunnableConfig:
        return {
            "configurable": {"thread_id": identifier},
        }

    async def messages(self, identifier):
        if self.agent is None:
            self.agent = build_agent(self.checkpointer)
        # 通过图恢复状态，兼容 Deep Agents 消息通道的增量存储方式。
        state = await self.agent.aget_state(self.config(identifier))
        return state.values.get("messages", [])

    def start(self, identifier, erp, text, page_context=None, attachments=()):
        if identifier in self.runs:
            raise AgentError("当前对话正在执行。", 409)
        if self.agent is None:
            self.agent = build_agent(self.checkpointer)
        run = Run()
        self.runs[identifier] = run
        queue = asyncio.Queue(maxsize=256)
        run.subscribers.add(queue)
        run.task = asyncio.create_task(
            self.execute(identifier, run, erp, text, page_context, attachments)
        )
        return self.events(run, queue)

    async def execute(self, identifier, run, erp, text, page_context, attachments):
        log = logger.bind(conversation_id=identifier)
        log.info("开始执行对话")
        try:
            assert self.agent is not None
            messages = await self.messages(identifier)
            incoming = await asyncio.to_thread(
                user_message, text, page_context, attachments
            )
            projection = StreamProjection()
            async for mode, event in self.agent.astream(
                {"messages": turn_input(messages, incoming)},
                self.config(identifier),
                context=TurnContext(erp),
                stream_mode=["messages", "updates"],
            ):
                for payload in projection.convert(mode, event):
                    run.publish(payload)
            await self.repository.touch(identifier)
            log.info("对话执行完成")
        except asyncio.CancelledError:
            log.info("对话执行已取消")
            run.publish({"type": "stopped"})
        except AgentError as exc:
            log.warning("对话执行失败", status=exc.status, problem_type=exc.type)
            run.publish(
                {
                    "type": "error",
                    "error": str(exc),
                    "trace_id": context.trace_id_ctx.get(),
                }
            )
        except Exception as exc:  # noqa: BLE001 -- 在事件输出前隐藏模型服务错误中的敏感信息
            log.opt(exception=exc).error("对话执行发生异常")
            # 不序列化模型服务的原始异常，其中可能包含凭据或业务数据。
            run.publish(
                {
                    "type": "error",
                    "error": "执行失败或超时，请检查模型配置后重试。",
                    "trace_id": context.trace_id_ctx.get(),
                }
            )
        finally:
            run.publish({"type": "done"})
            self.runs.pop(identifier, None)

    def subscribe(self, identifier):
        run = self.runs.get(identifier)
        if run is None:
            return self.completed()
        queue = asyncio.Queue(maxsize=256)
        run.subscribers.add(queue)
        return self.events(run, queue)

    async def completed(self):
        yield {"type": "done"}

    async def events(self, run, queue):
        try:
            while True:
                event = await queue.get()
                yield event
                if event["type"] == "done":
                    break
        finally:
            run.subscribers.discard(queue)

    async def stop(self, identifier):
        run = self.runs.get(identifier)
        if run and run.task:
            run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)
            # 任务可能在首次调度前就被取消，此时 execute 的 finally 尚未进入。
            if self.runs.get(identifier) is run:
                run.publish({"type": "stopped"})
                run.publish({"type": "done"})
                self.runs.pop(identifier, None)

    async def close(self):
        for identifier in tuple(self.runs):
            await self.stop(identifier)
