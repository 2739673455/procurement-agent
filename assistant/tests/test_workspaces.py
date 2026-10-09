"""真实 Docker 工作空间的用户隔离、会话默认目录与文件持久化检查。"""

import asyncio
import base64
import json
import os

import aiodocker
import pytest
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.message import TextBlock, ToolResultState
from test_runs import Model, response, run_turn, use_models

from app.contracts.sessions import AttachmentUpload
from app.runtime import bootstrap as runtime
from app.runtime.catalog import AgentCatalog
from app.services.sessions import SessionService


@pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_TESTS") != "1",
    reason="设置 RUN_DOCKER_TESTS=1 并提供 Docker 服务以运行沙箱集成测试",
)
def test_docker_user_isolation_and_session_directories(monkeypatch, tmp_path):
    """检查用户容器隔离、会话目录绑定、共享访问、删除及文件恢复。"""
    monkeypatch.setattr(runtime, "ROOT_DIR", tmp_path)
    host_file = tmp_path / "host-only.txt"
    host_file.write_text("host-only", encoding="utf-8")
    agents = {}
    initialize_agent = runtime.RuntimeAgent.__init__

    def capture_agent(agent, **kwargs):
        """记录框架装配的 Agent，检查运行中实际绑定的工具实例。"""
        initialize_agent(agent, **kwargs)
        agents[agent.state.session_id] = agent

    monkeypatch.setattr(runtime.RuntimeAgent, "__init__", capture_agent)

    async def tool_text(tool, **kwargs):
        """执行框架工具，校验成功并提取输出文本。"""
        result = tool.call(**kwargs)
        chunks = (
            [chunk async for chunk in result]
            if hasattr(result, "__aiter__")
            else [await result]
        )
        assert chunks and all(
            chunk.state != ToolResultState.ERROR for chunk in chunks
        ), chunks
        return "".join(
            block.text
            for chunk in chunks
            for block in chunk.content
            if isinstance(block, TextBlock)
        )

    async def scenario():
        models = [Model([response("完成")]), Model([response("完成")])]
        use_models(monkeypatch, models.copy())
        app = runtime.create_runtime(
            AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}"),
            catalog=AgentCatalog(),
        )
        container_names = set()
        async with app.router.lifespan_context(app):
            service = SessionService(app.state)
            owner = ("site", "owner")
            user_id, agent_id = service.agents.identity(owner)
            workspaces = []
            for session_owner in [owner, owner, ("site", "another-owner")]:
                session_user, session_agent = service.agents.identity(session_owner)
                session_id = (await service.create(session_owner))["id"]
                record = await service.storage.get_session(
                    session_user, session_agent, session_id
                )
                assert record is not None
                workspace = await app.state.workspace_manager.get_workspace(
                    session_user, session_agent, session_id, record.config.workspace_id
                )
                workspaces.append((session_id, workspace))
                container_names.add(f"as_ws_{workspace.workspace_id}")

            (session_id, first), (second_session, second), (_, other_user) = workspaces
            assert first is second
            assert first.workspace_id != other_user.workspace_id
            backend = first.get_backend()
            uploaded = await service.upload(
                owner,
                session_id,
                AttachmentUpload(
                    name="用户上传.txt",
                    data=base64.b64encode(b"uploaded-content").decode(),
                ),
            )
            assert await backend.read_file(uploaded["path"]) == b"uploaded-content"
            for current_session in [session_id, second_session]:
                events = await run_turn(service, current_session, message="开始")
                assert not [
                    event for event in events if event.get("finished_reason") == "error"
                ], events

            first_tools = {
                tool.name: tool
                for group in agents[session_id].toolkit.tool_groups
                for tool in group.tools
            }
            second_tools = {
                tool.name: tool
                for group in agents[second_session].toolkit.tool_groups
                for tool in group.tools
            }
            first_directory = f"/workspace/sessions/{session_id}"
            second_directory = f"/workspace/sessions/{second_session}"
            first_pwd, second_pwd = await asyncio.gather(
                tool_text(first_tools["Bash"], command="pwd"),
                tool_text(second_tools["Bash"], command="pwd"),
            )
            assert first_pwd.strip() == first_directory
            assert second_pwd.strip() == second_directory

            await tool_text(
                first_tools["Bash"], command="printf session-A > result.txt"
            )
            await tool_text(
                second_tools["Bash"], command="printf session-B > result.txt"
            )
            first_file = f"{first_directory}/result.txt"
            second_file = f"{second_directory}/result.txt"
            assert await backend.read_file(first_file) == b"session-A"
            assert await backend.read_file(second_file) == b"session-B"
            assert first_file in await tool_text(first_tools["Glob"], pattern="*.txt")
            assert second_file in await tool_text(second_tools["Glob"], pattern="*.txt")
            assert "session-A" in await tool_text(
                second_tools["Read"], file_path=first_file
            )
            written_file = f"{first_directory}/written.txt"
            await tool_text(
                first_tools["Write"], file_path=written_file, content="written"
            )
            assert "written" in await tool_text(
                first_tools["Read"], file_path=written_file
            )
            await tool_text(
                first_tools["Edit"],
                file_path=written_file,
                old_string="written",
                new_string="edited",
            )
            assert await backend.read_file(written_file) == b"edited"
            for name, arguments in (
                ("Read", {"file_path": "result.txt"}),
                ("Write", {"file_path": "relative.txt", "content": "text"}),
                (
                    "Edit",
                    {
                        "file_path": "result.txt",
                        "old_string": "session-A",
                        "new_string": "changed",
                    },
                ),
            ):
                result = await first_tools[name].call(**arguments)
                assert result.state == ToolResultState.ERROR
                assert "absolute path" in "".join(
                    block.text
                    for block in result.content
                    if isinstance(block, TextBlock)
                )
            assert await backend.read_file(first_file) == b"session-A"
            assert not await other_user.get_backend().file_exists(first_file)
            assert await backend.getcwd() == "/workspace"
            prompt = "\n".join(
                msg.get_text_content() or "" for msg in models[0].inputs[0]
            )
            assert first_directory in prompt
            assert "Read、Write、Edit 的 file_path 必须使用绝对路径" in prompt

            checks = await backend.exec_shell(
                [
                    "python3",
                    "-c",
                    (
                        "import json, os; print(json.dumps(["
                        "os.path.exists('/.dockerenv'), "
                        f"os.path.exists({str(host_file)!r}), "
                        "os.path.exists('/var/run/docker.sock'), "
                        "'DEEPSEEK_API_KEY' in os.environ, "
                        "'ASSISTANT_PG_PASSWORD' in os.environ]))"
                    ),
                ]
            )
            assert checks.exit_code == 0
            assert json.loads(checks.stdout) == [True, False, False, False, False]

            await app.state.workspace_manager.close(first.workspace_id)
            restored = await app.state.workspace_manager.get_workspace(
                user_id, agent_id, session_id, first.workspace_id
            )
            assert await restored.get_backend().read_file(first_file) == b"session-A"
            assert await restored.get_backend().read_file(second_file) == b"session-B"
            await service.delete(owner, session_id, "credential")
            assert not await restored.get_backend().file_exists(first_directory)
            assert await restored.get_backend().read_file(second_file) == b"session-B"

        async with aiodocker.Docker() as client:
            for name in container_names:
                with pytest.raises(aiodocker.DockerError) as error:
                    await client.containers.get(name)
                assert error.value.status == 404

    asyncio.run(scenario())
