"""公共 Runtime API 的生命周期、会话边界、事件和持久化行为。"""

import asyncio
from pathlib import Path

import pytest
from agentscope.app._service import SessionStatus
from agentscope.event import UserConfirmResultEvent
from agentscope.message import UserMsg
from test_runs import build_runtime, configured_catalog

from app.runtime import RunError, Runtime


def api_runtime(path: Path) -> Runtime:
    """公共 API 测试不装配需要 ERP 身份的业务工具。"""
    definitions = configured_catalog().definitions.model_copy(deep=True)
    for definition in definitions.agents.values():
        definition.tool_factories = []
        definition.builtin_tools = []
        definition.skills = []
        definition.mcps = []
    return build_runtime(path, catalog=configured_catalog(definitions, mcps={}))


def test_public_session_lifecycle_and_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = api_runtime(tmp_path)
        with pytest.raises(RuntimeError, match="async with"):
            await runtime.create("owner")
        async with runtime:
            with pytest.raises(RuntimeError, match="一次"):
                await runtime.__aenter__()
            with pytest.raises(RunError, match="未配置"):
                await runtime.create("owner", agent="missing")
            first = await runtime.create("owner", title="First")
            second = await runtime.create("owner", agent="procurement")
            assert first.agent_id == runtime.catalog.agent_id("owner", "procurement")
            assert first.config.workspace_id == second.config.workspace_id
            assert (await runtime.list("other"))["sessions"] == []
            assert len((await runtime.list("owner"))["sessions"]) == 2
            snapshot = await runtime.messages("owner", first.id)
            assert snapshot["messages"] == []
            assert not snapshot["running"] and not snapshot["resumable"]
            stream = await runtime.subscribe("owner", first.id)
            assert stream.media_type == "text/event-stream"
            with pytest.raises(RunError) as invalid:
                await runtime.rename("owner", first.id, " ")
            assert invalid.value.code == "invalid_input"
            await runtime.rename("owner", first.id, " Renamed ")
            assert (await runtime.get("owner", first.id)).config.name == "Renamed"
            await runtime.cancel("owner", first.id, {})
            assert (await runtime.messages("owner", first.id))[
                "status"
            ] == SessionStatus.IDLE
            await runtime.delete("owner", second.id, {})
            with pytest.raises(RunError):
                await runtime.get("owner", second.id)
        with pytest.raises(RuntimeError, match="async with"):
            await runtime.list("owner")
        async with api_runtime(tmp_path) as restarted:
            sessions = (await restarted.list("owner"))["sessions"]
            assert [(row["id"], row["title"]) for row in sessions] == [
                (first.id, "Renamed")
            ]
            assert (await restarted.messages("owner", first.id))[
                "status"
            ] == SessionStatus.IDLE
            await restarted.delete("owner", first.id, {})
            assert (await restarted.list("owner"))["sessions"] == []

    asyncio.run(scenario())


def test_all_public_operations_check_ownership(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with api_runtime(tmp_path) as runtime:
            row = await runtime.create("owner")
            operations = [
                lambda: runtime.get("intruder", row.id),
                lambda: runtime.messages("intruder", row.id),
                lambda: runtime.rename("intruder", row.id, "Stolen"),
                lambda: runtime.delete("intruder", row.id, {}),
                lambda: runtime.send(
                    "intruder", row.id, UserMsg(name="user", content="No"), {}
                ),
                lambda: runtime.resume("intruder", row.id, {}),
                lambda: runtime.interrupt("intruder", row.id, {}),
                lambda: runtime.cancel("intruder", row.id, {}),
                lambda: runtime.confirm(
                    "intruder",
                    row.id,
                    UserConfirmResultEvent(reply_id="missing", confirm_results=[]),
                    {},
                ),
                lambda: runtime.subscribe("intruder", row.id),
            ]
            for operation in operations:
                with pytest.raises(RunError) as denied:
                    await operation()
                assert denied.value.code == "not_found"
            assert (await runtime.get("owner", row.id)).config.name == "新对话"

    asyncio.run(scenario())


def test_reference_sessions_and_paginated_history(tmp_path: Path) -> None:
    """从原生存储写入边界数据，验证公共读取不泄漏内部会话且完整分页。"""
    from agentscope.app.storage import TeamOrigin

    from app.runtime.agents import AgentRegistry

    async def scenario() -> None:
        runtime = api_runtime(tmp_path)
        definitions = runtime.catalog.definitions
        worker = definitions.agents["procurement"].model_copy(deep=True)
        worker.name = "Worker"
        definitions.agents["worker"] = worker
        definitions.agents["procurement"].members = ["worker"]
        async with runtime:
            row = await runtime.create("owner")
            reference = AgentRegistry.reference_session_id("owner", "worker")
            with pytest.raises(RunError):
                await runtime.get("owner", reference)
            member = await runtime._storage.upsert_session(
                "owner", row.agent_id, row.config, origin=TeamOrigin()
            )
            with pytest.raises(RunError):
                await runtime.get("owner", member.id)
            assert [
                item["id"] for item in (await runtime.list("owner"))["sessions"]
            ] == [row.id]
            history = [
                UserMsg(
                    id=f"message-{i:03}",
                    name="user",
                    content=str(i),
                )
                for i in range(205)
            ]
            for message in history:
                await runtime._storage.upsert_message("owner", row.id, message)
            snapshot = await runtime.messages("owner", row.id)
            assert [msg.id for msg in snapshot["messages"]] == [
                msg.id for msg in history
            ]

    asyncio.run(scenario())


def test_shutdown_waits_for_inflight_session_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """关闭与会话写入竞争时，先完成写入，再关闭底层资源。"""

    async def scenario() -> None:
        runtime = api_runtime(tmp_path)
        await runtime.__aenter__()
        entered, release = asyncio.Event(), asyncio.Event()
        upsert = runtime._storage.upsert_session

        async def paused(*args, **kwargs):
            entered.set()
            await release.wait()
            return await upsert(*args, **kwargs)

        monkeypatch.setattr(runtime._storage, "upsert_session", paused)
        creation = asyncio.create_task(runtime.create("owner"))
        await asyncio.wait_for(entered.wait(), 5)
        shutdown = asyncio.create_task(runtime.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        row = await creation
        await shutdown
        with pytest.raises(RuntimeError, match="async with"):
            await runtime.get("owner", row.id)
        async with api_runtime(tmp_path) as restarted:
            assert (await restarted.get("owner", row.id)).id == row.id

    asyncio.run(scenario())


def test_sse_disconnect_and_runtime_shutdown_release_subscribers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """原生 SSE 在断开和总线关闭时退出 feeder，释放订阅。"""
    import json

    from agentscope.app.message_bus import MessageBusKeys

    async def scenario() -> None:
        runtime = api_runtime(tmp_path)
        await runtime.__aenter__()
        row = await runtime.create("owner")
        bus = runtime._message_bus
        subscribe = bus.subscribe
        ready = asyncio.Event()

        def tracked(key, **kwargs):
            return subscribe(key, **kwargs, on_ready=ready.set)

        monkeypatch.setattr(bus, "subscribe", tracked)
        frames = []

        async def consume(response):
            async for frame in response.body_iterator:
                frames.append(json.loads(frame.removeprefix("data: ")))

        key = MessageBusKeys.session_events(row.id)
        stream = await runtime.subscribe("owner", row.id)
        consumer = asyncio.create_task(consume(stream))
        await asyncio.wait_for(ready.wait(), 5)
        await bus.publish(key, {"type": "CUSTOM", "name": "check", "value": 1})
        while not frames:
            await asyncio.sleep(0)
        assert frames == [{"type": "CUSTOM", "name": "check", "value": 1}]
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert not any(
            t.get_name() == f"sse-feeder:{row.id}" for t in asyncio.all_tasks()
        )
        ready.clear()
        consumer = asyncio.create_task(
            consume(await runtime.subscribe("owner", row.id))
        )
        await asyncio.wait_for(ready.wait(), 5)
        await runtime.__aexit__(None, None, None)
        await asyncio.wait_for(consumer, 5)
        assert not any(
            t.get_name() == f"sse-feeder:{row.id}" for t in asyncio.all_tasks()
        )

    asyncio.run(scenario())


def test_exceptional_shutdown_cleans_active_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """宿主异常退出时原生运行注册器等待取消与模型客户端释放。"""
    from test_runs import Model, use_models

    async def scenario() -> None:
        entered = asyncio.Event()
        model = Model([], entered)
        use_models(monkeypatch, [model])
        runtime = api_runtime(tmp_path)
        with pytest.raises(ValueError, match="host failed"):
            async with runtime:
                row = await runtime.create("owner")
                await runtime.send(
                    "owner", row.id, UserMsg(name="user", content="Wait"), {}
                )
                await asyncio.wait_for(entered.wait(), 5)
                task = runtime._run_registry.get(row.id)
                raise ValueError("host failed")
        assert task is not None and task.done()
        model.client.close.assert_awaited_once()
        assert not runtime._contexts._contexts

    asyncio.run(scenario())
