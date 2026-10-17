"""通用运行控制、原生 Agent 团队协作与资源装配的关键集成测试。"""

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, Never

import pytest
from agentscope.app._service import SessionStatus
from agentscope.credential import CredentialFactory
from agentscope.event import ConfirmResult, UserConfirmResultEvent
from agentscope.message import Msg, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolChoice, ToolChunk
from test_runs import (
    Model,
    command,
    configured_catalog,
    response,
    run_turn,
    service_at,
    use_models,
    wait_for_idle,
    watch,
)

from app.runtime.errors import RunError
from app.tools import items as item_tools


def tool_response(
    name: str, arguments: dict[str, Any], identifier: str = "call1"
) -> ChatResponse:
    """构造模型的原生工具调用响应。"""
    reply = response()
    reply.append_tool_call(name=name, input=json.dumps(arguments), block_id=identifier)
    return reply


async def eventually(check: Callable[[], Awaitable[bool]]) -> None:
    """等待框架独立唤醒及持久化完成，超时即报告测试失败。"""
    async with asyncio.timeout(10):
        while not await check():
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("action", ["interrupt", "cancel"])
def test_interrupt_restart_resume_and_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str
) -> None:
    """中断和取消均保留上下文，重启后可按原生回复结束原因继续推理。"""

    async def scenario() -> None:
        entered = asyncio.Event()
        first = Model([], entered)
        resumed = Model([response("接着处理")])
        use_models(monkeypatch, [first, resumed])
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            await command(service, "send", identifier, message="分析物料")
            await asyncio.wait_for(entered.wait(), 5)
            await command(service, action, identifier)
            await wait_for_idle(
                service._runtime, service._user_id(("site", "owner")), identifier
            )
            state = await command(service, "messages", identifier)
            assert state["resumable"] and not state["running"]
            first.client.close.assert_awaited_once()
        async with service_at(tmp_path) as service:
            assert (await command(service, "messages", identifier))["resumable"]
            async with watch(service, identifier):
                await command(service, "resume", identifier, erp="fresh-credential")
                await service._runtime._app.state.chat_run_registry.get(identifier)
            history = (await command(service, "messages", identifier))["messages"]
            assert len([msg for msg in history if msg.role == "user"]) == 1
            assert any(
                msg.get_text_content() == "分析物料" for msg in resumed.inputs[0]
            )
            await command(service, "cancel", identifier)
            assert not (await command(service, "messages", identifier))["resumable"]
            with pytest.raises(RunError) as failure:
                await command(service, "resume", identifier)
            assert failure.value.code == "conflict"
            assert (await command(service, "messages", identifier))[
                "messages"
            ] == history

    asyncio.run(scenario())


def test_native_permission_confirmation_and_parked_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """权限确认使用保存的工具调用，并可中断等待确认的任务。"""

    async def scenario() -> None:
        called = []

        async def query_items(filters: list[list[str]]) -> ToolChunk:
            called.append(filters)
            return ToolChunk(content=[])

        monkeypatch.setattr(
            item_tools,
            "create_items_tool",
            lambda _: FunctionTool(
                query_items,
                permission=PermissionDecision(
                    behavior=PermissionBehavior.ASK, message="permission"
                ),
            ),
        )
        first = Model([response(tool=True)])
        resumed = Model([response("确认后完成")])
        use_models(
            monkeypatch, [first, resumed, Model([response(tool=True)]), Model([])]
        )
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            await run_turn(service, identifier, message="查询")
            state = await command(service, "messages", identifier)
            assert state["status"] == SessionStatus.AWAITING_PERMISSION
            request = state["confirmations"][0]
            # 工具执行参数以持久化调用为准，忽略浏览器回传的参数改动。
            forged = request.tool_calls[0].model_copy(
                update={"input": '{"filters":[["item_code","=","forged"]]}'}
            )
            result = UserConfirmResultEvent(
                reply_id=request.reply_id,
                confirm_results=[ConfirmResult(confirmed=True, tool_call=forged)],
            )
            # 空提交、重复 ID 和其他任务的调用均不能消耗待确认请求。
            for submitted_calls in (
                [],
                [forged, forged],
                [forged.model_copy(update={"id": "unknown-call"})],
            ):
                with pytest.raises(RunError):
                    await command(
                        service,
                        "confirm",
                        identifier,
                        confirmation=UserConfirmResultEvent(
                            reply_id=request.reply_id,
                            confirm_results=[
                                ConfirmResult(confirmed=True, tool_call=call)
                                for call in submitted_calls
                            ],
                        ),
                    )
                assert not called
            # 取消后，等待权限确认的调用可通过确认操作继续执行。
            await command(service, "cancel", identifier)
            assert (await command(service, "messages", identifier))[
                "status"
            ] == SessionStatus.AWAITING_PERMISSION
            await command(service, "confirm", identifier, confirmation=result)
            await service._runtime._app.state.chat_run_registry.get(identifier)
            assert called == [[["item_code", "like", "%bolt%"]]]
            await run_turn(service, identifier, message="再查询")
            assert (await command(service, "messages", identifier))["confirmations"]
            await command(service, "interrupt", identifier)

            async def interrupted() -> bool:
                snapshot = await command(service, "messages", identifier)
                return snapshot["resumable"] and not snapshot["confirmations"]

            await eventually(interrupted)
            state = await command(service, "messages", identifier)
            assert state["resumable"] and not state["confirmations"]
            with pytest.raises(RunError):
                await command(service, "confirm", identifier, confirmation=result)
            assert called == [[["item_code", "like", "%bolt%"]]]

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["interrupt", "cancel"])
def test_native_team_context_controls_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str
) -> None:
    """控制负责人回复不会停止成员；成员结果仍会原生唤醒负责人，身份不串用户。"""

    async def scenario() -> None:
        from agentscope.app._service import _chat

        from app.tools import items

        owners = []

        async def query(erp: str, doctype: str, **kwargs: Any) -> dict[str, Any]:
            assert doctype == "Item"
            owners.append(erp)
            return {"data": []}

        monkeypatch.setattr(items.api, "list_documents", query)
        member_entered = asyncio.Event()
        leader_entered = asyncio.Event()
        release_member = asyncio.Event()
        models = []
        leader_count = 0
        member_count = 0

        async def get_model(*args: Any, **kwargs: Any) -> Model:
            """根据实际可用工具驱动负责人 Agent 和成员 Agent，覆盖框架自动唤醒路径。"""
            model = Model([])
            record = await args[2].resolve_credential(args[0], args[1].credential_id)
            model.credential = CredentialFactory.from_dict(record.data)
            models.append(model)

            async def call(
                model_name: str,
                messages: list[Msg],
                tools: list[dict[str, Any]] | None = None,
                tool_choice: ToolChoice | None = None,
                **kwargs: Any,
            ) -> ChatResponse:
                nonlocal leader_count, member_count
                model.inputs.append(deepcopy(messages))
                model.tools = tools
                assert tools is not None
                names = {tool["function"]["name"] for tool in tools}
                if "Bash" in names:
                    leader_count += 1
                    if leader_count == 1:
                        return tool_response(
                            "TeamCreate",
                            {"name": "采购查询", "description": "检索物料"},
                        )
                    if leader_count == 2:
                        user_id = service._user_id(("site", "owner"))
                        agent_id = service._runtime._app.state.catalog.agent_id(
                            user_id, "item_researcher"
                        )
                        return tool_response(
                            "AgentInvite",
                            {
                                "target": f"物料查询助手@{agent_id[:8]}",
                                "prompt": "查螺栓并报告",
                            },
                            "invite",
                        )
                    if leader_count == 3:
                        leader_entered.set()
                        await asyncio.Event().wait()
                    return response("负责人完成")
                assert "query_items" in names and "TeamCreate" not in names
                member_count += 1
                if member_count == 1:
                    member_entered.set()
                    await release_member.wait()
                if member_count == 1:
                    return response(tool=True)
                if member_count == 2:
                    return tool_response(
                        "TeamSay",
                        {"content": "物料查询完成", "to": "采购助手"},
                        "report",
                    )
                return response("成员完成")

            model._call_api = call
            return model

        monkeypatch.setattr(_chat, "get_model", get_model)
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            user_id = service._user_id(("site", "owner"))
            initial_root = await service._runtime._storage.get_session(
                user_id, "", identifier
            )
            assert initial_root is not None
            await command(
                service, "send", identifier, message="组织查询", erp="leader-client"
            )
            await asyncio.wait_for(member_entered.wait(), 8)
            await asyncio.wait_for(leader_entered.wait(), 8)
            state = await command(service, "messages", identifier)
            assert state["running"] and len(state["members"]) == 1
            member_id = state["members"][0]["session_id"]
            assert initial_root.team_id is None
            await command(service, action, identifier, erp="fresh-client")
            await wait_for_idle(service._runtime, user_id, identifier)
            state = await command(service, "messages", identifier)
            assert not state["running"] and state["resumable"]
            assert state["members"][0]["status"] == SessionStatus.RUNNING
            member_task = service._runtime._run_registry.get(member_id)
            assert member_task is not None and not member_task.done()
            release_member.set()

            async def finished() -> bool:
                history, _ = await service._runtime._storage.list_messages(
                    user_id, member_id, limit=1
                )
                return (
                    bool(history and history[-1].finished_reason == "completed")
                    and (await command(service, "messages", identifier))["messages"][
                        -1
                    ].get_text_content()
                    == "负责人完成"
                    and not (await command(service, "messages", identifier))["running"]
                )

            await eventually(finished)
            assert owners == ["leader-client"]
            assert (await command(service, "list", owner=("site", "other")))[
                "sessions"
            ] == []
            assert len((await command(service, "list"))["sessions"]) == 1
            await command(service, "cancel", identifier)
            assert (
                await service._runtime._storage.get_session(user_id, "", member_id)
                is not None
            )
            assert (await command(service, "messages", identifier))["messages"]
            await command(service, "delete", identifier)
            assert (
                await service._runtime._storage.get_session(user_id, "", member_id)
                is None
            )
            assert (
                await service._runtime._storage.get_agent(
                    user_id, state["members"][0]["agent_id"]
                )
                is not None
            )
        for model in models:
            model.client.close.assert_awaited_once()

    asyncio.run(scenario())


def test_native_mcp_and_skill_role_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """调用真实 STDIO MCP，并验证完整 Skill 文件及角色能力隔离。"""

    async def scenario() -> None:
        from agentscope.agent import InjectionConfig
        from agentscope.app.message_bus import InMemoryMessageBus
        from agentscope.app.storage import AsyncSQLAlchemyStorage
        from agentscope.app.workspace_manager import (
            IsolationPolicy,
            LocalWorkspaceManager,
        )

        from app.config.mcp import MCPDefinitions
        from app.runtime.bootstrap import create_runtime
        from app.services.sessions import SessionService

        server = tmp_path / "mcp_server.py"
        server.write_text(
            'from mcp.server.fastmcp import FastMCP\nmcp = FastMCP("test")\n@mcp.tool()\ndef echo(value: str) -> str:\n    return "mcp:" + value\nmcp.run()\n'
        )
        catalog = configured_catalog()
        definitions = catalog.definitions.model_copy(deep=True)
        definitions.agents["procurement"].mcps = ["echo"]
        # 为另一个角色收敛能力，验证资源不会跨角色泄漏。
        definitions.agents["item_researcher"].tool_factories = []
        definitions.agents["item_researcher"].skills = []
        catalog = configured_catalog(
            definitions=definitions,
            mcps=MCPDefinitions.model_validate(
                {
                    "servers": {
                        "echo": {
                            "is_stateful": True,
                            "mcp_config": {
                                "type": "stdio_mcp",
                                "command": sys.executable,
                                "args": [str(server)],
                            },
                        }
                    }
                }
            ).servers,
        )
        model = Model([])

        async def call(
            model_name: str,
            messages: list[Msg],
            tools: list[dict[str, Any]] | None = None,
            tool_choice: ToolChoice | None = None,
            **kwargs: Any,
        ) -> ChatResponse:
            model.inputs.append(messages)
            model.tools = tools
            assert tools is not None
            if len(model.inputs) == 1:
                mcp_tool = next(
                    tool for tool in tools if "echo" in tool["function"]["name"]
                )
                return tool_response(mcp_tool["function"]["name"], {"value": "hello"})
            return response("完成")

        model._call_api = call
        other = Model([response("资源检查完成")])
        queued_models = [model]
        use_models(monkeypatch, queued_models)
        app = create_runtime(
            AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}"),
            message_bus=InMemoryMessageBus(),
            injection_config=InjectionConfig(timezone="Asia/Shanghai"),
            context_ttl_seconds=3600,
            workspace_manager=LocalWorkspaceManager(
                str(tmp_path / "workspaces"), isolation=IsolationPolicy.PER_USER
            ),
            catalog=catalog,
        )
        async with app:
            service = SessionService(app)
            identifier = (await command(service, "create"))["id"]
            events = await run_turn(service, identifier, message="MCP 测试")
            assert not [
                event for event in events if event.get("finished_reason") == "error"
            ], [event for event in events if event.get("finished_reason") == "error"]
            state = await command(service, "messages", identifier)
            confirmation = state["confirmations"][0]
            user_id = service._user_id(("site", "owner"))
            agent_id = service._catalog.agent_id(
                user_id, service._catalog.definitions.default
            )
            row = await service._runtime._storage.get_session(
                user_id, agent_id, identifier
            )
            assert row is not None
            workspace = await app.workspace_manager.get_workspace(
                user_id, agent_id, identifier, row.config.workspace_id
            )
            add_mcp = workspace.add_mcp

            async def unavailable(*args: Any, **kwargs: Any) -> Never:
                """模拟中断收尾期间 MCP 连接不可用。"""
                raise ConnectionError("MCP unavailable")

            monkeypatch.setattr(workspace, "add_mcp", unavailable)
            queued_models.append(Model([]))
            await command(service, "interrupt", identifier)

            async def interrupted() -> bool:
                return (await command(service, "messages", identifier))["resumable"]

            await eventually(interrupted)
            monkeypatch.setattr(workspace, "add_mcp", add_mcp)
            queued_models.extend(
                [
                    Model(
                        [
                            tool_response(
                                confirmation.tool_calls[0].name,
                                {"value": "hello"},
                                "retry",
                            )
                        ]
                    ),
                    Model([response("MCP 完成")]),
                    other,
                ]
            )
            await command(service, "resume", identifier)
            await service._runtime._app.state.chat_run_registry.get(identifier)
            confirmation = (await command(service, "messages", identifier))[
                "confirmations"
            ][0]
            await command(
                service,
                "confirm",
                identifier,
                confirmation=UserConfirmResultEvent(
                    reply_id=confirmation.reply_id,
                    confirm_results=[
                        ConfirmResult(confirmed=True, tool_call=call)
                        for call in confirmation.tool_calls
                    ],
                ),
            )
            await service._runtime._app.state.chat_run_registry.get(identifier)
            history = (await command(service, "messages", identifier))["messages"]
            assert "mcp:hello" in str(history[-1].content)
            user_id = service._user_id(("site", "owner"))
            agent_id = service._catalog.agent_id(
                user_id, service._catalog.definitions.default
            )
            row = await service._runtime._storage.get_session(
                user_id, agent_id, identifier
            )
            assert row is not None
            workspace = await app.workspace_manager.get_workspace(
                user_id, agent_id, identifier, row.config.workspace_id
            )
            skills = await workspace.list_skills(agent_id=agent_id)
            assert [skill.name for skill in skills] == ["item-query"]
            assert await workspace.get_backend().file_exists(
                workspace.get_backend().join_path(
                    skills[0].dir, "references", "fields.md"
                )
            )
            # 会话入口由服务端配置决定，切换入口后验证角色资源隔离。
            monkeypatch.setattr(catalog.definitions, "default", "item_researcher")
            researcher = (await command(service, "create"))["id"]
            await run_turn(service, researcher, message="检查角色资源")
            assert other.tools is not None
            assert not any(
                "echo" in tool["function"]["name"]
                or tool["function"]["name"] == "query_items"
                for tool in other.tools
            )
            assert skills[0].dir not in str(other.inputs)
            assert not any(
                isinstance(block, ToolCallBlock) and block.state == "asking"
                for block in history[-1].content
            )
            assert any(
                isinstance(block, ToolResultBlock) for block in history[-1].content
            )
            installed = await workspace.list_mcps(
                agent_id=agent_id, session_id=identifier
            )
        assert not installed[0].is_connected

    asyncio.run(scenario())


def test_mcp_yaml_uses_service_keys_as_client_names(tmp_path: Path) -> None:
    """配置名成为原生客户端名称，HTTP 和 STDIO 参数按原生结构传递。"""
    from app.config.loader import load_yaml
    from app.config.mcp import MCPDefinitions

    path = tmp_path / "mcp.yaml"
    path.write_text(
        """servers:
  files:
    is_stateful: true
    mcp_config:
      type: stdio_mcp
      command: python3
      args: [/workspace/mcp/server.py]
  documents:
    is_stateful: false
    mcp_config:
      type: http_mcp
      url: https://example.com/mcp
    enable_tools: [search]
    execution_timeout: 30
""",
        encoding="utf-8",
    )
    data = load_yaml(path)
    assert isinstance(data, dict)
    configured = MCPDefinitions.model_validate(data)
    assert {key: client.name for key, client in configured.servers.items()} == {
        "files": "files",
        "documents": "documents",
    }
    assert "name" not in data["servers"]["files"]
    assert configured.servers["documents"].enable_tools == ["search"]
    assert configured.servers["documents"].execution_timeout == 30


def test_tool_factory_configuration_rejects_invalid_references() -> None:
    """在启动时拒绝不存在的工厂、非函数引用和不接受上下文的函数。"""

    definitions = configured_catalog().definitions.model_copy(deep=True)
    for path in (
        "items",
        "app.tools.items:missing_factory",
        "app.tools.items:logger",
        "app.services.inputs:user_message",
    ):
        definitions.agents["procurement"].tool_factories = [path]
        with pytest.raises(ValueError, match="工具工厂"):
            configured_catalog(definitions=definitions)
