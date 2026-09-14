"""参考 dataagent 实现的会话运行管理，任务生命周期独立于客户端连接。"""

import asyncio
import json
from dataclasses import dataclass, field

from langchain_core.runnables import RunnableConfig

from app.agent.runtime import TurnContext, build_agent, turn_input
from app.errors import AgentError


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


class AgentManager:
    def __init__(self, checkpointer, repository):
        self.checkpointer = checkpointer
        self.repository = repository
        self.agent = None
        self.runs: dict[str, Run] = {}
        # 串行执行归属校验、启动和删除操作，防止已删除的会话重新启动。
        self.lock = asyncio.Lock()

    @staticmethod
    def config(identifier) -> RunnableConfig:
        return {
            "configurable": {"thread_id": identifier},
        }

    async def messages(self, identifier):
        checkpoint = await self.checkpointer.aget_tuple(self.config(identifier))
        return (
            checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
            if checkpoint
            else []
        )

    def start(self, identifier, erp, text):
        if identifier in self.runs:
            raise AgentError("当前对话正在执行。", 409)
        if self.agent is None:
            self.agent = build_agent(self.checkpointer)
        run = Run()
        self.runs[identifier] = run
        queue = asyncio.Queue(maxsize=256)
        run.subscribers.add(queue)
        run.task = asyncio.create_task(self.execute(identifier, run, erp, text))
        return self.events(run, queue)

    async def execute(self, identifier, run, erp, text):
        try:
            assert self.agent is not None
            messages = await self.messages(identifier)
            async for event in self.agent.astream(
                {"messages": turn_input(messages, text)},
                self.config(identifier),
                context=TurnContext(erp),
                stream_mode="custom",
            ):
                run.publish(event)
            await self.repository.touch(identifier)
        except asyncio.CancelledError:
            run.publish({"type": "stopped"})
        except AgentError as exc:
            run.publish({"type": "error", "error": str(exc)})
        except Exception:  # noqa: BLE001 -- 在事件输出前隐藏模型服务错误中的敏感信息
            # 不序列化模型服务的原始异常，其中可能包含凭据或业务数据。
            run.publish(
                {"type": "error", "error": "执行失败或超时，请检查模型配置后重试。"}
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
        yield 'data: {"type":"done"}\n\n'

    async def events(self, run, queue):
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                if event["type"] == "done":
                    break
        finally:
            run.subscribers.discard(queue)

    async def stop(self, identifier):
        run = self.runs.get(identifier)
        if run and run.task:
            run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)

    async def close(self):
        for identifier in tuple(self.runs):
            await self.stop(identifier)
