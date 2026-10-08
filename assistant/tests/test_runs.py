import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.credential import OpenAICredential
from agentscope.formatter import OpenAIChatFormatter
from agentscope.model import ChatModelBase, ChatResponse
from pydantic import SecretStr

from app.agent.runtime import create_runtime
from app.contracts.conversations import Command
from app.errors.agent import AgentError
from app.services.conversations import ConversationService


class Model(ChatModelBase):
    def __init__(self, calls, entered=None):
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
        workspace_dir=path / "workspaces",
    )
    async with app.router.lifespan_context(app):
        yield ConversationService(app.state)


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
        Command(
            sid=SecretStr("test"), action=action, conversation_id=identifier, **kwargs
        ),
        owner,
        erp,
    )


async def collect(stream):
    async with asyncio.timeout(10):
        return [event async for event in stream]


def use_models(monkeypatch, models):
    from agentscope.app._service import _chat

    async def get_model(*args, **kwargs):
        return models.pop(0)

    monkeypatch.setattr(_chat, "get_model", get_model)


def test_tool_history_restored_and_credentials_bound(monkeypatch, tmp_path):
    async def scenario():
        first = Model([response("查询中", True), response("找到物料")])
        second = Model([response("继续回复")])
        use_models(monkeypatch, [first, second])
        from app.agent.tools import items

        owners = []

        def query(erp, params):
            owners.append(erp)
            return {"items": [{"item_code": "BOLT"}]}

        monkeypatch.setattr(items.items, "query_items", query)
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            events = await collect(
                await command(service, "send", identifier, message="查询螺丝")
            )
            assert not [e for e in events if e["type"] == "error"], events
            assert owners == ["credential-a"]
            history = (await command(service, "messages", identifier))["messages"]
            assert [m["role"] for m in history] == [
                "user",
                "assistant",
                "tool",
                "assistant",
            ]
            assert history[-1]["content"] == "找到物料"
            user_id, _ = service.identity(("site", "owner"))
            credentials = await service.storage.list_credentials(user_id)
            assert "api_key" not in str([record.data for record in credentials])
            assert "bash" not in str(first.tools).lower()
            assert "query_items" in str(first.tools)
            first.client.close.assert_awaited_once()
        async with service_at(tmp_path) as service:
            await command(service, "rename", identifier, title="物料查询")
            events = await collect(
                await command(
                    service, "send", identifier, message="继续", erp="credential-b"
                )
            )
            assert not [e for e in events if e["type"] == "error"], events
            assert any(m.get_text_content() == "查询螺丝" for m in second.inputs[0])
            assert (
                len((await command(service, "messages", identifier))["messages"]) == 6
            )

    asyncio.run(scenario())


def test_cancel_then_new_turn_and_double_send(monkeypatch, tmp_path):
    async def scenario():
        entered = asyncio.Event()
        model = Model([], entered)
        use_models(monkeypatch, [model, Model([response("恢复")])])
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            stream = await command(service, "send", identifier, message="问题")
            collector = asyncio.create_task(collect(stream))
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(AgentError) as conflict:
                await command(service, "send", identifier, message="重复")
            assert conflict.value.status == 409
            await command(service, "stop", identifier)
            events = await collector
            assert events[-2:] == [{"type": "stopped"}, {"type": "done"}]
            assert not (await command(service, "messages", identifier))["running"]
            model.client.close.assert_awaited_once()
            events = await collect(
                await command(service, "send", identifier, message="新问题")
            )
            assert not [e for e in events if e["type"] == "error"], events

    asyncio.run(scenario())


def test_owner_isolation_delete_and_disconnect(monkeypatch, tmp_path):
    async def scenario():
        entered = asyncio.Event()
        use_models(monkeypatch, [Model([], entered)])
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            for action in ["messages", "send", "stop", "delete", "subscribe", "rename"]:
                with pytest.raises(AgentError) as denied:
                    await command(
                        service,
                        action,
                        identifier,
                        owner=("site", "other"),
                        message="no",
                    )
                assert denied.value.status == 404
            stream = await command(service, "send", identifier, message="问题")
            watcher = asyncio.create_task(collect(stream))
            await asyncio.wait_for(entered.wait(), 5)
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
            task = service.runtime.chat_run_registry.get(identifier)
            assert task is not None and not task.done()
            subscribed = await command(service, "subscribe", identifier)
            collector = asyncio.create_task(collect(subscribed))
            await command(service, "delete", identifier)
            assert (await collector)[-1] == {"type": "done"}
            assert (await command(service, "list"))["conversations"] == []
            user_id, agent_id = service.identity(("site", "owner"))
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

        from app.clients.chat_model import ChatCompletionsModel

        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            user_id, agent_id = service.identity(("site", "owner"))
            record = await service.storage.get_session(user_id, agent_id, identifier)
            assert record is not None and record.config.chat_model_config is not None
            model = await get_model(
                user_id,
                record.config.chat_model_config,
                service.runtime.resource_access_service,
            )
            assert isinstance(model, ChatCompletionsModel)
            await model.client.close()

    asyncio.run(scenario())


def test_concurrent_users_get_separate_erp_clients(monkeypatch, tmp_path):
    async def scenario():
        from app.agent.tools import items

        owners = []

        def query(erp, params):
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
            first = await command(service, "send", a, message="query", erp="client-A")
            second = await command(
                service,
                "send",
                b,
                message="query",
                owner=("site", "other"),
                erp="client-B",
            )
            results = await asyncio.gather(collect(first), collect(second))
            assert sorted(owners) == ["client-A", "client-B"]
            assert all(
                not any(event["type"] == "error" for event in events)
                for events in results
            )

    asyncio.run(scenario())


def test_cancel_during_tool_preserves_result_pairs(monkeypatch, tmp_path):
    async def scenario():
        from agentscope.message import ToolCallBlock, ToolResultBlock
        from agentscope.permission import PermissionBehavior, PermissionDecision
        from agentscope.tool import FunctionTool, ToolChunk

        from app.agent import runtime

        entered = asyncio.Event()

        async def query_items(query: str) -> ToolChunk:
            entered.set()
            await asyncio.Event().wait()
            return ToolChunk(content=[])

        monkeypatch.setattr(
            runtime,
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
            collector = asyncio.create_task(
                collect(await command(service, "send", identifier, message="query"))
            )
            await asyncio.wait_for(entered.wait(), 5)
            await command(service, "stop", identifier)
            events = await collector
            assert events[-2:] == [{"type": "stopped"}, {"type": "done"}]
            user_id, agent_id = service.identity(("site", "owner"))
            record = await service.storage.get_session(user_id, agent_id, identifier)
            assert record is not None
            blocks = [block for msg in record.state.context for block in msg.content]
            assert (
                {b.id for b in blocks if isinstance(b, ToolCallBlock)}
                == {b.id for b in blocks if isinstance(b, ToolResultBlock)}
                == {"call1"}
            )
            events = await collect(
                await command(service, "send", identifier, message="继续")
            )
            assert not [e for e in events if e["type"] == "error"], events

    asyncio.run(scenario())


def test_late_subscription_reports_persisted_failure(monkeypatch, tmp_path):
    async def scenario():
        use_models(monkeypatch, [Model([])])
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            stream = await command(service, "send", identifier, message="失败")
            task = service.runtime.chat_run_registry.get(identifier)
            assert task is not None
            await task
            events = await collect(stream)
            assert any(e["type"] == "error" for e in events), events
            assert events[-1] == {"type": "done"}

    asyncio.run(scenario())
