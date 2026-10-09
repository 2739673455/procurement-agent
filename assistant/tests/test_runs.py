import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.app.message_bus import MessageBusKeys
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import IsolationPolicy, LocalWorkspaceManager
from agentscope.credential import OpenAICredential
from agentscope.event import AgentEvent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import ToolResultBlock
from agentscope.model import ChatModelBase, ChatResponse
from pydantic import SecretStr, TypeAdapter

from app.agents.procurement.definition import TOOL_FACTORIES
from app.contracts.sessions import Command
from app.errors.agent import AgentError
from app.runtime.bootstrap import RuntimeAgent, create_runtime
from app.runtime.catalog import AgentCatalog
from app.runtime.models import model_client_scope, register_model_client
from app.services.sessions import SessionService


class Model(ChatModelBase):
    """模拟框架模型，提供预设回复或等待取消，记录输入与客户端释放。"""

    def __init__(self, calls, entered=None):
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
        self.inputs = []
        self.tools = []

    async def _call_api(
        self, model_name, messages, tools=None, tool_choice=None, **kwargs
    ):
        """记录消息和工具，按测试配置等待取消或返回预设回复。"""
        self.client.close.assert_not_awaited()
        self.inputs.append(deepcopy(messages))
        self.tools = tools
        if self.entered:
            self.entered.set()
            await asyncio.Event().wait()
        return self.calls.pop(0)


def response(text=None, tool=False):
    value = ChatResponse(content=[], is_last=True)
    if text:
        value.append_text(text)
    if tool:
        value.append_tool_call(
            name="query_items", input='{"query":"bolt"}', block_id="call1"
        )
    return value


@asynccontextmanager
async def service_at(path):
    app = create_runtime(
        AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{path / 'state.db'}"),
        catalog=AgentCatalog(TOOL_FACTORIES),
        workspace_manager=LocalWorkspaceManager(
            str(path / "workspaces"), isolation=IsolationPolicy.PER_USER
        ),
    )
    async with app.router.lifespan_context(app):
        yield SessionService(app.state)


async def command(
    service,
    action,
    identifier=None,
    *,
    owner=("site", "owner"),
    erp="credential-a",
    **kwargs,
):
    return await service.execute(
        Command(sid=SecretStr("test"), action=action, session_id=identifier, **kwargs),
        owner,
        erp,
    )


@asynccontextmanager
async def watch(service, identifier, *, owner=("site", "owner")):
    """消费真实框架 SSE 响应，连接建立后交给测试，退出时模拟浏览器断开。"""
    response = await command(service, "subscribe", identifier, owner=owner)
    assert response.media_type == "text/event-stream"
    assert response.headers["X-Accel-Buffering"] == "no"
    ready = asyncio.Event()
    bus = service.runtime.message_bus
    subscribe = bus.subscribe

    def mark_ready(key, **kwargs):
        """记录框架订阅就绪时机，避免测试投递早于订阅建立。"""
        return subscribe(key, **kwargs, on_ready=ready.set)

    bus.subscribe = mark_ready
    events = []

    async def consume():
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


async def run_turn(service, identifier, *, owner=("site", "owner"), **kwargs):
    """先建立订阅再发送，验证启动结果并等待后台运行完成。"""
    async with watch(service, identifier, owner=owner) as events:
        result = await command(service, "send", identifier, owner=owner, **kwargs)
        assert result.model_dump() == {"status": "started", "session_id": identifier}
        task = service.runtime.chat_run_registry.get(identifier)
        assert task is not None
        await asyncio.wait_for(task, 10)
    return events


def use_models(monkeypatch, models):
    from agentscope.app._service import _chat

    async def get_model(*args, **kwargs):
        model = models.pop(0)
        register_model_client(model.client)
        return model

    monkeypatch.setattr(_chat, "get_model", get_model)


def test_tool_history_restored_and_credentials_bound(monkeypatch, tmp_path):
    async def scenario():
        first = Model([response("查询中", True), response("找到物料")])
        second = Model([response("继续回复")])
        use_models(monkeypatch, [first, second])
        from app.agents.procurement.tools import items

        owners = []

        def query(erp, query: str, *, offset: int, limit: int):
            owners.append(erp)
            assert (query, offset, limit) == ("bolt", 0, 20)
            return {"items": [{"item_code": "BOLT"}]}

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


def test_cancel_then_new_turn_and_double_send(monkeypatch, tmp_path):
    async def scenario():
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


def test_owner_isolation_delete_and_disconnect(monkeypatch, tmp_path):
    async def scenario():
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


def test_native_model_factory_resolves_server_reference(tmp_path):
    async def scenario():
        from agentscope.app._service import get_model

        from app.runtime.models import ChatCompletionsModel

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
                assert isinstance(model, ChatCompletionsModel)
                assert not model.client.is_closed()
            assert model.client.is_closed()

    asyncio.run(scenario())


def test_concurrent_users_get_separate_erp_clients(monkeypatch, tmp_path):
    async def scenario():
        from app.agents.procurement.tools import items

        owners = []

        def query(erp, query: str, *, offset: int, limit: int):
            owners.append(erp)
            return {"items": []}

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
                await command(
                    service,
                    "send",
                    b,
                    message="query",
                    owner=("site", "other"),
                    erp="client-B",
                )
                tasks = [service.runtime.chat_run_registry.get(id) for id in (a, b)]
                assert all(task is not None for task in tasks)
                await asyncio.wait_for(asyncio.gather(*tasks), 10)
            results = [first, second]
            assert sorted(owners) == ["client-A", "client-B"]
            assert all(
                not any(event.get("finished_reason") == "error" for event in events)
                for events in results
            )

    asyncio.run(scenario())


def test_cancel_during_tool_preserves_result_pairs(monkeypatch, tmp_path):
    async def scenario():
        from agentscope.message import ToolCallBlock, ToolResultBlock
        from agentscope.permission import PermissionBehavior, PermissionDecision
        from agentscope.tool import FunctionTool, ToolChunk

        from app.agents.procurement import definition

        entered = asyncio.Event()

        async def query_items(query: str) -> ToolChunk:
            entered.set()
            await asyncio.Event().wait()
            return ToolChunk(content=[])

        monkeypatch.setattr(
            definition,
            "create_items_tool",
            lambda _: FunctionTool(
                query_items,
                permission=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW, message="test"
                ),
            ),
        )
        use_models(
            monkeypatch, [Model([response(tool=True)]), Model([response("继续")])]
        )
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            async with watch(service, identifier) as events:
                await command(service, "send", identifier, message="query")
                await asyncio.wait_for(entered.wait(), 5)
                await command(service, "cancel", identifier)
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
                == {"call1"}
            )
            events = await run_turn(service, identifier, message="继续")
            assert not [e for e in events if e.get("finished_reason") == "error"], (
                events
            )

    asyncio.run(scenario())


def test_late_subscription_reports_persisted_failure(monkeypatch, tmp_path):
    async def scenario():
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


def test_queued_reply_keeps_model_client_open(monkeypatch, tmp_path):
    """同一次运行处理队列消息时复用客户端，在全部回复结束后释放。"""

    async def scenario():
        from agentscope.message import HintBlock

        model = Model([response("第一条回复"), response("补充回复")])
        use_models(monkeypatch, [model])
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            call_api = model._call_api

            async def enqueue_hint(*args, **kwargs):
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


def test_agent_setup_failure_closes_all_model_clients(monkeypatch, tmp_path):
    """Agent 装配失败时，也释放已创建的主模型和备用模型客户端。"""

    async def scenario():
        primary = Model([])
        fallback = Model([])
        use_models(monkeypatch, [primary, fallback])

        def fail_setup(self, **kwargs):
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
