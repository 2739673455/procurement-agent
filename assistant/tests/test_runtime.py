"""通用运行控制、原生团队协作与资源装配的关键集成测试。"""

import asyncio
import json
import sys
from copy import deepcopy

import pytest
from agentscope.app._service import SessionStatus
from agentscope.event import ConfirmResult, UserConfirmResultEvent
from agentscope.mcp import MCPClient, StdioMCPConfig
from agentscope.message import ToolCallBlock, ToolResultBlock
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolChunk
from test_runs import Model, command, response, run_turn, service_at, use_models, watch

from app.agents.procurement import definition
from app.errors.agent import AgentError
from app.runtime.context import CONTROL_KEY


def tool_response(name, arguments, identifier="call1"):
    """构造模型的原生工具调用响应。"""
    reply = response()
    reply.append_tool_call(name=name, input=json.dumps(arguments), block_id=identifier)
    return reply


async def eventually(check):
    """等待框架独立唤醒及持久化完成，超时即报告测试失败。"""
    async with asyncio.timeout(10):
        while not await check():
            await asyncio.sleep(0.02)


def test_interrupt_restart_resume_and_cancel(monkeypatch, tmp_path):
    """重启后恢复已保存上下文，取消保留历史并禁止无新输入恢复。"""

    async def scenario():
        entered = asyncio.Event()
        first = Model([], entered)
        resumed = Model([response("接着处理")])
        use_models(monkeypatch, [first, resumed])
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            await command(service, "send", identifier, message="分析物料")
            await asyncio.wait_for(entered.wait(), 5)
            await command(service, "interrupt", identifier)
            state = await command(service, "messages", identifier)
            assert state["resumable"] and not state["running"]
            first.client.close.assert_awaited_once()
        async with service_at(tmp_path) as service:
            assert (await command(service, "messages", identifier))["resumable"]
            async with watch(service, identifier):
                await command(service, "resume", identifier, erp="fresh-credential")
                await service.runtime.chat_run_registry.get(identifier)
            history = (await command(service, "messages", identifier))["messages"]
            assert len([msg for msg in history if msg.role == "user"]) == 1
            assert any(
                msg.get_text_content() == "分析物料" for msg in resumed.inputs[0]
            )
            await command(service, "cancel", identifier)
            assert not (await command(service, "messages", identifier))["resumable"]
            with pytest.raises(AgentError) as failure:
                await command(service, "resume", identifier)
            assert failure.value.status == 409
            assert (await command(service, "messages", identifier))[
                "messages"
            ] == history

    asyncio.run(scenario())


def test_native_permission_confirmation_and_parked_interrupt(monkeypatch, tmp_path):
    """权限确认使用保存的工具调用，并可中断等待确认的任务。"""

    async def scenario():
        called = []

        async def query_items(query: str) -> ToolChunk:
            called.append(query)
            return ToolChunk(content=[])

        monkeypatch.setattr(
            definition,
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
            # 浏览器的参数不能覆盖数据库中已经审核的工具调用。
            forged = request.tool_calls[0].model_copy(
                update={"input": '{"query":"forged"}'}
            )
            result = UserConfirmResultEvent(
                reply_id=request.reply_id,
                confirm_results=[ConfirmResult(confirmed=True, tool_call=forged)],
            )
            await command(service, "confirm", identifier, confirmation=result)
            await service.runtime.chat_run_registry.get(identifier)
            assert called == ["bolt"]
            await run_turn(service, identifier, message="再查询")
            assert (await command(service, "messages", identifier))["confirmations"]
            await command(service, "interrupt", identifier)
            state = await command(service, "messages", identifier)
            assert state["resumable"] and not state["confirmations"]
            with pytest.raises(AgentError):
                await command(service, "confirm", identifier, confirmation=result)
            assert called == ["bolt"]

    asyncio.run(scenario())


def test_native_team_context_controls_and_cleanup(monkeypatch, tmp_path):
    """独立成员使用负责人认证身份；整个团队中断、恢复及取消不会串用户。"""

    async def scenario():
        from agentscope.app._service import _chat

        from app.agents.procurement.tools import items
        from app.runtime.models import register_model_client

        owners = []
        monkeypatch.setattr(
            items.items,
            "query_items",
            lambda erp, query, *, offset, limit: owners.append(erp) or {"items": []},
        )
        member_entered = asyncio.Event()
        release_member = asyncio.Event()
        models = []
        leader_count = 0
        member_count = 0

        async def get_model(*args, **kwargs):
            """根据实际可用工具驱动负责人和成员，覆盖框架自动唤醒路径。"""
            model = Model([])
            models.append(model)
            register_model_client(model.client)

            async def call(
                model_name, messages, tools=None, tool_choice=None, **kwargs
            ):
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
                        user_id = service.agents.catalog.user_id(("site", "owner"))
                        agent_id = service.agents.catalog.agent_id(
                            user_id, "item_researcher"
                        )
                        return tool_response(
                            "AgentInvite",
                            {
                                "target": f"item_researcher@{agent_id[:8]}",
                                "prompt": "查螺栓并报告",
                            },
                            "invite",
                        )
                    return response("负责人完成")
                assert "query_items" in names and "TeamCreate" not in names
                member_count += 1
                if member_count == 1:
                    member_entered.set()
                    await release_member.wait()
                if member_count <= 2:
                    return response(tool=True)
                if member_count == 3:
                    return tool_response(
                        "TeamSay",
                        {"content": "物料查询完成", "to": "procurement_assistant"},
                        "report",
                    )
                return response("成员完成")

            model._call_api = call
            return model

        monkeypatch.setattr(_chat, "get_model", get_model)
        async with service_at(tmp_path) as service:
            identifier = (await command(service, "create"))["id"]
            user_id = service.agents.catalog.user_id(("site", "owner"))
            initial_root = await service.storage.get_session(user_id, "", identifier)
            await command(
                service, "send", identifier, message="组织查询", erp="leader-client"
            )
            await asyncio.wait_for(member_entered.wait(), 8)
            state = await command(service, "messages", identifier)
            assert state["running"] and len(state["members"]) == 1
            member_id = state["members"][0]["session_id"]
            assert initial_root.team_id is None
            await service.runs.stop(
                user_id,
                initial_root,
                {"erpnext": "leader-client"},
                intent="interrupted",
            )

            async def stopped():
                return not (await command(service, "messages", identifier))["running"]

            await eventually(stopped)
            user_id = service.agents.catalog.user_id(("site", "owner"))
            root = await service.storage.get_session(user_id, "", identifier)
            member = await service.storage.get_session(user_id, "", member_id)
            assert root.state.middle_context[CONTROL_KEY] == "interrupted"
            assert member.state.middle_context[CONTROL_KEY] == "interrupted"
            release_member.set()
            await command(service, "resume", identifier, erp="fresh-client")

            async def finished():
                history, _ = await service.storage.list_messages(
                    user_id, member_id, limit=1
                )
                return (
                    bool(history and history[-1].finished_reason == "completed")
                    and not (await command(service, "messages", identifier))["running"]
                )

            await eventually(finished)
            assert owners == ["fresh-client"]
            assert (await command(service, "list", owner=("site", "other")))[
                "sessions"
            ] == []
            assert len((await command(service, "list"))["sessions"]) == 1
            await command(service, "cancel", identifier)
            assert await service.storage.get_session(user_id, "", member_id) is None
            assert await service.storage.get_agent(user_id, member.agent_id) is not None
            assert (await command(service, "messages", identifier))["messages"]
        for model in models:
            model.client.close.assert_awaited_once()

    asyncio.run(scenario())


def test_native_mcp_and_skill_role_resources(monkeypatch, tmp_path):
    """调用真实 STDIO MCP，并验证完整 Skill 文件及角色能力隔离。"""

    async def scenario():
        from agentscope.app.storage import AsyncSQLAlchemyStorage
        from agentscope.app.workspace_manager import (
            IsolationPolicy,
            LocalWorkspaceManager,
        )

        from app.runtime.bootstrap import create_runtime
        from app.runtime.catalog import AgentCatalog, MCPDefinitions
        from app.services.sessions import SessionService

        server = tmp_path / "mcp_server.py"
        server.write_text(
            'from mcp.server.fastmcp import FastMCP\nmcp = FastMCP("test")\n@mcp.tool()\ndef echo(value: str) -> str:\n    return "mcp:" + value\nmcp.run()\n'
        )
        client = MCPClient(
            name="echo",
            is_stateful=True,
            mcp_config=StdioMCPConfig(command=sys.executable, args=[str(server)]),
        )
        catalog = AgentCatalog(definition.TOOL_FACTORIES)
        definitions = catalog.definitions.model_copy(deep=True)
        definitions.agents["procurement"].mcps = ["echo"]
        catalog = AgentCatalog(
            definition.TOOL_FACTORIES,
            definitions=definitions,
            mcps=MCPDefinitions(servers={"echo": client}),
        )
        model = Model([])

        async def call(model_name, messages, tools=None, tool_choice=None, **kwargs):
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
        other = Model([response("文件分析")])
        queued_models = [model]
        use_models(monkeypatch, queued_models)
        app = create_runtime(
            AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}"),
            workspace_manager=LocalWorkspaceManager(
                str(tmp_path / "workspaces"), isolation=IsolationPolicy.PER_USER
            ),
            catalog=catalog,
        )
        async with app.router.lifespan_context(app):
            service = SessionService(app.state)
            identifier = (await command(service, "create"))["id"]
            events = await run_turn(service, identifier, message="MCP 测试")
            assert not [
                event for event in events if event.get("finished_reason") == "error"
            ], [event for event in events if event.get("finished_reason") == "error"]
            state = await command(service, "messages", identifier)
            confirmation = state["confirmations"][0]
            user_id, agent_id = service.agents.identity(("site", "owner"))
            row = await service.storage.get_session(user_id, agent_id, identifier)
            workspace = await app.state.workspace_manager.get_workspace(
                user_id, agent_id, identifier, row.config.workspace_id
            )
            add_mcp = workspace.add_mcp

            async def unavailable(*args, **kwargs):
                """模拟中断收尾期间 MCP 连接不可用。"""
                raise ConnectionError("MCP unavailable")

            monkeypatch.setattr(workspace, "add_mcp", unavailable)
            queued_models.append(Model([]))
            await command(service, "interrupt", identifier)
            assert (await command(service, "messages", identifier))["resumable"]
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
            await service.runtime.chat_run_registry.get(identifier)
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
            await service.runtime.chat_run_registry.get(identifier)
            history = (await command(service, "messages", identifier))["messages"]
            assert "mcp:hello" in str(history[-1].content)
            user_id, agent_id = service.agents.identity(("site", "owner"))
            row = await service.storage.get_session(user_id, agent_id, identifier)
            workspace = await app.state.workspace_manager.get_workspace(
                user_id, agent_id, identifier, row.config.workspace_id
            )
            skills = await workspace.list_skills(agent_id=agent_id)
            assert [skill.name for skill in skills] == ["item-query"]
            assert await workspace.get_backend().file_exists(
                workspace.get_backend().join_path(
                    skills[0].dir, "references", "fields.md"
                )
            )
            analyst = (await command(service, "create", agent_key="file_analyst"))["id"]
            await run_turn(service, analyst, message="分析文件")
            assert other.tools is not None
            assert not any(
                "echo" in tool["function"]["name"]
                or tool["function"]["name"] == "query_items"
                for tool in other.tools
            )
            assert "item-query" not in str(other.inputs)
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
