import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Never, cast
from unittest.mock import AsyncMock

import pytest
from agentscope.app.message_bus import MessageBusKeys
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import IsolationPolicy, LocalWorkspaceManager
from agentscope.credential import OpenAICredential
from agentscope.event import AgentEvent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg, ToolResultBlock
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.tool import ToolChoice
from pydantic import SecretStr, TypeAdapter

from app.clients.erpnext.client import ERPNext
from app.clients.erpnext.items import Filters
from app.contracts.sessions import Command
from app.errors.agent import AgentError
from app.runtime.bootstrap import RuntimeAgent, create_runtime
from app.runtime.catalog import AgentCatalog
from app.runtime.models import _model_clients, model_client_scope
from app.services.sessions import SessionService


class Model(ChatModelBase):
    """模拟框架模型，提供预设回复或等待取消，记录输入与客户端释放。"""

    def __init__(
        self, calls: list[ChatResponse], entered: asyncio.Event | None = None
    ) -> None:
        """设置预设回复；entered 用于通知测试模型已进入可取消的等待阶段。"""
        super().__init__(
            OpenAICredential(api_key=SecretStr("test")),
            "test",
            self.Parameters(),
            max_retries=0,
        )
        self.formatter = OpenAIChatFormatter()
        self.calls = calls
        self.entered = entered
        self.client = SimpleNamespace(close=AsyncMock())
        self.inputs: list[list[Msg]] = []
        self.tools: list[dict[str, Any]] | None = []

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """记录消息和工具，按测试配置等待取消或返回预设回复。"""
        self.client.close.assert_not_awaited()
        self.inputs.append(deepcopy(messages))
        self.tools = tools
        if self.entered:
            self.entered.set()
            await asyncio.Event().wait()
        return self.calls.pop(0)


def response(text: str | None = None, tool: bool = False) -> ChatResponse:
    value = ChatResponse(content=[], is_last=True)
    if text:
        value.append_text(text)
    if tool:
        value.append_tool_call(
            name="query_items",
            input='{"filters":[["item_code","like","%bolt%"]]}',
            block_id="call1",
        )
    return value


@asynccontextmanager
async def service_at(path: Path) -> AsyncGenerator[SessionService]:
    app = create_runtime(
        AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{path / 'state.db'}"),
        catalog=AgentCatalog(),
        workspace_manager=LocalWorkspaceManager(
            str(path / "workspaces"), isolation=IsolationPolicy.PER_USER
        ),
    )
    async with app.router.lifespan_context(app):
        yield SessionService(app.state)


async def command(
    service: SessionService,
    action: str,
    identifier: str | None = None,
    *,
    owner: tuple[str, str] = ("site", "owner"),
    erp: str = "credential-a",
    **kwargs: Any,
) -> Any:
    return await service.execute(
        Command.model_validate(
            {"sid": "test", "action": action, "session_id": identifier, **kwargs}
        ),
        owner,
        cast(ERPNext, erp),
    )


@asynccontextmanager
async def watch(
    service: SessionService,
    identifier: str,
    *,
    owner: tuple[str, str] = ("site", "owner"),
) -> AsyncGenerator[list[dict[str, Any]]]:
    """消费真实框架 SSE 响应，连接建立后交给测试，退出时模拟浏览器断开。"""
    response = await command(service, "subscribe", identifier, owner=owner)
    assert response.media_type == "text/event-stream"
    assert response.headers["X-Accel-Buffering"] == "no"
    ready = asyncio.Event()
    bus = service.runtime.message_bus
    subscribe = bus.subscribe

    def mark_ready(key: str, **kwargs: Any) -> AsyncGenerator[dict[str, Any]]:
        """记录框架订阅就绪时机，避免测试投递早于订阅建立。"""
        return subscribe(key, **kwargs, on_ready=ready.set)

    bus.subscribe = mark_ready
    events: list[dict[str, Any]] = []

    async def consume() -> None:
        """解析框架编码的原生事件，忽略心跳注释。"""
        async for frame in response.body_iterator:
            for line in frame.splitlines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    TypeAdapter(AgentEvent).validate_python(event)
                    assert "_entry_id" not in event
                    events.append(event)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(ready.wait(), 5)
        bus.subscribe = subscribe
        yield events
        await asyncio.sleep(0)
        assert not consumer.done(), "运行结束后订阅应保持连接"
    finally:
        bus.subscribe = subscribe
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


async def run_turn(
    service: SessionService,
    identifier: str,
    *,
    owner: tuple[str, str] = ("site", "owner"),
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """先建立订阅再发送，验证启动结果并等待后台运行完成。"""
    async with watch(service, identifier, owner=owner) as events:
        result = await command(service, "send", identifier, owner=owner, **kwargs)
        assert result.model_dump() == {"status": "started", "session_id": identifier}
        task = service.runtime.chat_run_registry.get(identifier)
        assert task is not None
        await asyncio.wait_for(task, 10)
    return events


def use_models(monkeypatch: pytest.MonkeyPatch, models: list[Model]) -> None:
    from agentscope.app._service import _chat

    async def get_model(*args: Any, **kwargs: Any) -> Model:
        model = models.pop(0)
        stack = _model_clients.get()
        assert stack is not None
        stack.push_async_callback(model.client.close)
        return model

    monkeypatch.setattr(_chat, "get_model", get_model)


def test_tool_history_restored_and_credentials_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        first = Model([response("查询中", True), response("找到物料")])
        second = Model([response("继续回复")])
        use_models(monkeypatch, [first, second])
        from app.tools import items

        owners = []

        async def query(
            erp: str,
            *,
            filters: Filters | None,
            limit_start: int,
            limit_page_length: int,
            **kwargs: Any,
        ) -> dict[str, Any]:
            owners.append(erp)
            assert filters == [["item_code", "like", "%bolt%"]]
            assert (limit_start, limit_page_length) == (0, 20)
            return {"data": [{"item_code": "BOLT"}]}

        monkeypatch.setattr(items.items, "query_items", query)
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            events = await run_turn(service, identifier, message="查询螺丝")
            assert not [e for e in events if e.get("finished_reason") == "error"], (
                events
            )
            assert owners == ["credential-a"]
            history = (await command(service, "messages", identifier))["messages"]
            assert [m.role for m in history] == ["user", "assistant"]
            assert history[-1].get_text_content() == "查询中\n找到物料"
            assert any(
                isinstance(block, ToolResultBlock) for block in history[-1].content
            )
            user_id, _ = service.agents.identity(("site", "owner"))
            credentials = await service.storage.list_credentials(user_id)
            assert "api_key" not in str([record.data for record in credentials])
            assert "bash" in str(first.tools).lower()
            assert "query_items" in str(first.tools)
            first.client.close.assert_awaited_once()
        async with service_at(tmp_path) as service:
            await command(service, "rename", identifier, title="物料查询")
            events = await run_turn(
                service, identifier, message="继续", erp="credential-b"
            )
            assert not [e for e in events if e.get("finished_reason") == "error"], (
                events
            )
            assert any(m.get_text_content() == "查询螺丝" for m in second.inputs[0])
            assert (
                len((await command(service, "messages", identifier))["messages"]) == 4
            )

    asyncio.run(scenario())


def test_cancel_then_new_turn_and_double_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        model = Model([], entered)
        use_models(monkeypatch, [model, Model([response("恢复")])])
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            async with watch(service, identifier) as events:
                await command(service, "send", identifier, message="问题")
                await asyncio.wait_for(entered.wait(), 5)
                with pytest.raises(AgentError) as conflict:
                    await command(service, "send", identifier, message="重复")
                assert conflict.value.status == 409
                await command(service, "cancel", identifier)
                assert not (await command(service, "messages", identifier))["running"]
                model.client.close.assert_awaited_once()
                await command(service, "send", identifier, message="新问题")
                task = service.runtime.chat_run_registry.get(identifier)
                assert task is not None
                await asyncio.wait_for(task, 10)
            ends = [e for e in events if e["type"] == "REPLY_END"]
            assert [e["finished_reason"] for e in ends] == ["interrupted", "completed"]

    asyncio.run(scenario())


def test_owner_isolation_delete_and_disconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        use_models(monkeypatch, [Model([], entered)])
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            for action in [
                "messages",
                "send",
                "cancel",
                "delete",
                "subscribe",
                "rename",
            ]:
                with pytest.raises(AgentError) as denied:
                    await command(
                        service,
                        action,
                        identifier,
                        owner=("site", "other"),
                        message="no",
                    )
                assert denied.value.status == 404
            async with watch(service, identifier):
                await command(service, "send", identifier, message="问题")
                await asyncio.wait_for(entered.wait(), 5)
            task = service.runtime.chat_run_registry.get(identifier)
            assert task is not None and not task.done()
            async with watch(service, identifier) as events:
                await command(service, "delete", identifier)
            assert any(
                e["type"] == "REPLY_END" and e["finished_reason"] == "interrupted"
                for e in events
            ), events
            assert (await command(service, "list"))["sessions"] == []
            user_id, agent_id = service.agents.identity(("site", "owner"))
            assert (
                await service.storage.get_session(user_id, agent_id, identifier) is None
            )
            assert await service.storage.list_messages(user_id, identifier) == (
                [],
                False,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["deepseek", "openai", "dashscope"])
def test_native_model_factory_resolves_server_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str
) -> None:
    async def scenario() -> None:
        from agentscope.app._service import get_model
        from agentscope.credential import CredentialFactory
        from agentscope.model import (
            DashScopeChatModel,
            DeepSeekChatModel,
            OpenAIChatModel,
        )

        from app.config.agents import cfg

        settings = cfg.models[cfg.agents[cfg.default].model]
        credential = CredentialFactory.from_dict(
            {
                "type": f"{provider}_credential",
                "api_key": "server-only-secret",
                "base_url": "https://model.invalid",
            }
        )
        monkeypatch.setattr(settings, "credential", credential)
        monkeypatch.setattr(settings, "model", "gpt-4o")
        monkeypatch.setattr(settings, "context_size", 12345)
        monkeypatch.setattr(settings, "image_inputs", provider == "deepseek")
        monkeypatch.setattr(settings, "params", {"max_tokens": 128})

        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            user_id, agent_id = service.agents.identity(("site", "owner"))
            record = await service.storage.get_session(user_id, agent_id, identifier)
            assert record is not None and record.config.chat_model_config is not None
            async with model_client_scope():
                model = await get_model(
                    user_id,
                    record.config.chat_model_config,
                    service.runtime.resource_access_service,
                )
                assert type(model) is credential.get_chat_model_class()
                assert isinstance(
                    model, (DeepSeekChatModel, OpenAIChatModel, DashScopeChatModel)
                )
                assert model.credential is credential
                assert model.parameters.model_dump()["max_tokens"] == 128
                assert model.context_size == 12345
                assert ("image/*" in model.formatter.input_types) == (
                    provider == "deepseek"
                )
                assert model.client.timeout == settings.client_kwargs["timeout"]
                assert model.max_retries == model.client.max_retries == 0
                assert str(model.client.base_url).rstrip("/") == "https://model.invalid"
                assert not model.client.is_closed()
                credentials = await service.storage.list_credentials(user_id)
                stored = str([record.data for record in credentials])
                assert "server-only-secret" not in stored and "api_key" not in stored
            assert model.client.is_closed()

    asyncio.run(scenario())


def test_concurrent_users_get_separate_erp_clients(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        from app.tools import items

        owners = []

        async def query(erp: str, **kwargs: Any) -> dict[str, Any]:
            owners.append(erp)
            return {"data": []}

        monkeypatch.setattr(items.items, "query_items", query)
        use_models(
            monkeypatch,
            [
                Model([response(tool=True), response("A")]),
                Model([response(tool=True), response("B")]),
            ],
        )
        async with service_at(tmp_path) as service:
            a = (await command(service, "create"))["id"]
            b = (await command(service, "create", owner=("site", "other")))["id"]
            async with (
                watch(service, a) as first,
                watch(service, b, owner=("site", "other")) as second,
            ):
                await command(service, "send", a, message="query", erp="client-A")
                first_task = service.runtime.chat_run_registry.get(a)
                await command(
                    service,
                    "send",
                    b,
                    message="query",
                    owner=("site", "other"),
                    erp="client-B",
                )
                tasks = [first_task, service.runtime.chat_run_registry.get(b)]
                assert all(task is not None for task in tasks)
                await asyncio.wait_for(asyncio.gather(*tasks), 10)
            results = [first, second]
            assert sorted(owners) == ["client-A", "client-B"]
            assert all(
                not any(event.get("finished_reason") == "error" for event in events)
                for events in results
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["interrupt", "cancel"])
@pytest.mark.parametrize("count", [1, 2])
def test_stop_during_tools_preserves_result_pairs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str, count: int
) -> None:
    async def scenario() -> None:
        from agentscope.message import ToolCallBlock, ToolResultBlock
        from agentscope.permission import PermissionBehavior, PermissionDecision
        from agentscope.tool import FunctionTool, ToolChunk

        from app.tools import items as item_tools

        entered = asyncio.Event()
        started = 0
        exited = 0

        async def query_items(filters: list[list[str]]) -> ToolChunk:
            nonlocal started, exited
            started += 1
            if started == count:
                entered.set()
            try:
                await asyncio.Event().wait()
                return ToolChunk(content=[])
            finally:
                exited += 1

        monkeypatch.setattr(
            item_tools,
            "create_items_tool",
            lambda _: FunctionTool(
                query_items,
                permission=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW, message="test"
                ),
            ),
        )
        calls = response(tool=True)
        if count == 2:
            calls.append_tool_call(
                name="query_items", input='{"filters":[]}', block_id="call2"
            )
        use_models(monkeypatch, [Model([calls]), Model([response("继续")])])
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            async with watch(service, identifier) as events:
                await command(service, "send", identifier, message="query")
                await asyncio.wait_for(entered.wait(), 5)
                await command(service, action, identifier)
                assert started == exited == count
            assert any(
                e["type"] == "REPLY_END" and e["finished_reason"] == "interrupted"
                for e in events
            ), events
            user_id, agent_id = service.agents.identity(("site", "owner"))
            record = await service.storage.get_session(user_id, agent_id, identifier)
            assert record is not None
            blocks = [block for msg in record.state.context for block in msg.content]
            assert (
                {b.id for b in blocks if isinstance(b, ToolCallBlock)}
                == {b.id for b in blocks if isinstance(b, ToolResultBlock)}
                == {f"call{i + 1}" for i in range(count)}
            )
            events = await run_turn(service, identifier, message="继续")
            assert not [e for e in events if e.get("finished_reason") == "error"], (
                events
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["complete", "interrupt", "cancel", "tool_stop"])
def test_background_tool_state_delivery_and_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str
) -> None:
    """后台工具计入运行状态，迟到结果自动唤醒，停止操作等待工具清理。"""
    from agentscope.app._manager._background_task_manager import ToolStop
    from agentscope.app.middleware import ToolOffloadMiddleware
    from agentscope.message import Base64Source, DataBlock, HintBlock, TextBlock
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.tool import FunctionTool, ToolChunk

    from app.tools import items as item_tools

    initialize = ToolOffloadMiddleware.__init__

    def initialize_fast(self: ToolOffloadMiddleware, *args: Any, **kwargs: Any) -> None:
        """缩短框架等待时间，测试仍经过真实后台转移和消息投递。"""
        initialize(self, *args, **kwargs, timeout_secs=0.05)

    monkeypatch.setattr(ToolOffloadMiddleware, "__init__", initialize_fast)

    async def scenario() -> None:
        release = asyncio.Event()
        cancelled = asyncio.Event()
        cleanup = asyncio.Event()
        exited = asyncio.Event()

        async def query_items(filters: list[list[str]]) -> ToolChunk:
            """等待测试释放结果；取消后留出可观察的工具清理阶段。"""
            try:
                await release.wait()
                return ToolChunk(
                    content=[
                        TextBlock(text="LATE_RESULT"),
                        DataBlock(
                            source=Base64Source(media_type="image/png", data="cG5n")
                        ),
                    ]
                )
            except asyncio.CancelledError:
                cancelled.set()
                await cleanup.wait()
                raise
            finally:
                exited.set()

        monkeypatch.setattr(
            item_tools,
            "create_items_tool",
            lambda _: FunctionTool(
                query_items,
                permission=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW, message="test"
                ),
            ),
        )
        first = Model([response(tool=True), response("工具在后台执行")])
        following = Model([response("后续回复")])
        models = [first, following]
        use_models(monkeypatch, models)
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            await run_turn(service, identifier, message="查询")
            history = await command(service, "messages", identifier)
            assert history["running"]
            assert not history["resumable"]
            assert [
                task["tool_name"] for task in history["background_tasks"].values()
            ] == ["query_items"]
            assert first.tools is not None
            assert "ToolStop" in {tool["function"]["name"] for tool in first.tools}
            with pytest.raises(AgentError, match="正在执行"):
                await command(service, "send", identifier, message="不能抢跑")

            if action == "complete":
                release.set()
                async with asyncio.timeout(5):
                    while True:
                        history = await command(service, "messages", identifier)
                        if (
                            not history["running"]
                            and history["messages"][-1].get_text_content() == "后续回复"
                        ):
                            break
                        await asyncio.sleep(0.01)
                hints = [
                    block
                    for message in following.inputs[0]
                    for block in message.content
                    if isinstance(block, HintBlock)
                ]
                assert any("call1" in (hint.source or "") for hint in hints)
                blocks = [
                    block
                    for hint in hints
                    if isinstance(hint.hint, list)
                    for block in hint.hint
                ]
                assert any(
                    isinstance(b, TextBlock) and "LATE_RESULT" in b.text for b in blocks
                )
                assert any(
                    isinstance(b, DataBlock)
                    and isinstance(b.source, Base64Source)
                    and b.source.data == "cG5n"
                    for b in blocks
                )
            else:
                if action == "tool_stop":
                    tools = await service.runtime.background_task_manager.list_tools(
                        identifier
                    )
                    await cast(ToolStop, tools[0])(
                        next(iter(history["background_tasks"]))
                    )
                stopping = asyncio.create_task(
                    command(
                        service,
                        "cancel" if action == "tool_stop" else action,
                        identifier,
                    )
                )
                await asyncio.wait_for(cancelled.wait(), 5)
                await asyncio.sleep(0.01)
                assert not stopping.done()
                cleanup.set()
                await asyncio.wait_for(stopping, 5)
                assert exited.is_set()
                history = await command(service, "messages", identifier)
                assert not history["running"]
                assert not history["background_tasks"]
                assert not following.inputs
                if action == "interrupt":
                    assert history["resumable"]
                    await command(service, "resume", identifier)
                    task = service.runtime.chat_run_registry.get(identifier)
                    assert task is not None
                    await asyncio.wait_for(task, 5)
                else:
                    assert not history["resumable"]
                    await run_turn(service, identifier, message="新任务")
            assert not models
            assert not (await command(service, "messages", identifier))["running"]

    asyncio.run(scenario())


def test_late_subscription_reports_persisted_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        model = Model([])
        use_models(monkeypatch, [model])
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            await command(service, "send", identifier, message="失败")
            task = service.runtime.chat_run_registry.get(identifier)
            assert task is not None
            await task
            async with watch(service, identifier) as events:
                assert not events
                history = (await command(service, "messages", identifier))["messages"]
                assert history[-1].finished_reason == "error" and history[-1].error
            model.client.close.assert_awaited_once()

    asyncio.run(scenario())


def test_queued_reply_keeps_model_client_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同一次运行处理队列消息时复用客户端，在全部回复结束后释放。"""

    async def scenario() -> None:
        from agentscope.message import HintBlock

        model = Model([response("第一条回复"), response("补充回复")])
        use_models(monkeypatch, [model])
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            call_api = model._call_api

            async def enqueue_hint(*args: Any, **kwargs: Any) -> ChatResponse:
                """在首条回复生成过程中投递消息，触发框架继续当前运行。"""
                if not model.inputs:
                    await service.runtime.message_bus.queue_push(
                        MessageBusKeys.inbox(identifier),
                        HintBlock(source="test", hint="请补充说明").model_dump(
                            mode="json"
                        ),
                    )
                return await call_api(*args, **kwargs)

            monkeypatch.setattr(model, "_call_api", enqueue_hint)
            events = await run_turn(service, identifier, message="问题")
            assert not [
                event for event in events if event.get("finished_reason") == "error"
            ], events
            assert len(model.inputs) == 2
            assert "请补充说明" in str(model.inputs[1])
            history = (await command(service, "messages", identifier))["messages"]
            assert history[-1].get_text_content() == "补充回复"
            model.client.close.assert_awaited_once()

    asyncio.run(scenario())


def test_agent_setup_failure_closes_all_model_clients(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Agent 装配失败时，也释放已创建的主模型和备用模型客户端。"""

    async def scenario() -> None:
        primary = Model([])
        fallback = Model([])
        use_models(monkeypatch, [primary, fallback])

        def fail_setup(self: RuntimeAgent, **kwargs: Any) -> Never:
            """在模型创建后中断 Agent 装配。"""
            raise ValueError("Agent setup failed")

        monkeypatch.setattr(RuntimeAgent, "__init__", fail_setup)
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            user_id, agent_id = service.agents.identity(("site", "owner"))
            record = await service.storage.get_session(user_id, agent_id, identifier)
            assert record is not None
            record.config.fallback_chat_model_config = record.config.chat_model_config
            await service.storage.upsert_session(
                user_id, agent_id, record.config, session_id=identifier
            )
            await run_turn(service, identifier, message="问题")
            history = (await command(service, "messages", identifier))["messages"]
            assert history[-1].finished_reason == "error" and history[-1].error
            assert not primary.inputs and not fallback.inputs
            primary.client.close.assert_awaited_once()
            fallback.client.close.assert_awaited_once()

    asyncio.run(scenario())


def test_upload_workspace_ownership_and_cleanup(tmp_path: Path) -> None:
    """同名上传覆盖，附件按会话查找，删除会话只清理所属文件。"""
    import base64
    from pathlib import Path

    async def scenario() -> None:
        async with service_at(tmp_path) as service:
            first = (await command(service, "create"))["id"]
            second = (await command(service, "create"))["id"]
            data = {
                "name": "报价.csv",
                "data": base64.b64encode("型号,数量\nA,3".encode()).decode(),
            }
            uploaded = await command(service, "upload", first, upload=data)
            replacement = {**data, "data": base64.b64encode(b"updated").decode()}
            duplicate = await command(service, "upload", first, upload=replacement)
            assert uploaded == duplicate
            assert "id" not in uploaded
            assert Path(uploaded["path"]).read_bytes() == b"updated"
            assert Path(uploaded["path"]).parts[-4:] == (
                "sessions",
                first,
                "attachments",
                "报价.csv",
            )
            user_id, row = await service._get_session(("site", "owner"), first)
            loaded = await service.attachments.load(
                user_id, row, ["报价.csv", "报价.csv"]
            )
            assert loaded == [uploaded]
            _, other = await service._get_session(("site", "owner"), second)
            with pytest.raises(AgentError):
                await service.attachments.load(user_id, other, ["报价.csv"])
            independent = await command(service, "upload", second, upload=data)
            assert independent["path"] != uploaded["path"]
            assert Path(independent["path"]).read_text() == "型号,数量\nA,3"
            with pytest.raises(AgentError):
                await command(
                    service, "upload", first, owner=("site", "intruder"), upload=data
                )
            for name in ("../escape", "..", "nested/file", r"nested\file"):
                with pytest.raises(AgentError):
                    await command(
                        service, "upload", first, upload={**data, "name": name}
                    )
                with pytest.raises(AgentError):
                    await service.attachments.load(user_id, row, [name])
            with pytest.raises(AgentError):
                await command(
                    service, "upload", first, upload={**data, "data": "!invalid"}
                )
            assert Path(uploaded["path"]).read_bytes() == b"updated"
            await command(service, "delete", first)
            assert not Path(uploaded["path"]).exists()
            assert Path(independent["path"]).read_text() == "型号,数量\nA,3"

    asyncio.run(scenario())
