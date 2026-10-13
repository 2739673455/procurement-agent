"""资源声明恢复、MCP 连接复用及沙箱 Skill 并发安装检查。"""

import asyncio
from pathlib import Path

import pytest
from agentscope.mcp import MCPClient
from agentscope.skill import Skill
from agentscope.workspace import LocalWorkspace, WorkspaceBase
from test_runs import configured_catalog

from app.runtime.resources import equip_resources


def test_mcp_reuse_reconfigure_and_restore(tmp_path: Path) -> None:
    """MCP 按会话复用，配置变更与重启恢复使用框架保存的声明。"""

    async def scenario() -> None:
        catalog = configured_catalog()
        definition = catalog.definitions.agents["procurement"].model_copy(deep=True)
        definition.skills = []
        definition.mcps = ["echo"]
        client = MCPClient.model_validate(
            {
                "name": "echo",
                "is_stateful": False,
                "mcp_config": {"type": "http_mcp", "url": "https://example.com/mcp"},
            }
        )
        catalog = configured_catalog(mcps={"echo": client})
        directory = str(tmp_path / "workspace")
        workspace = LocalWorkspace(workdir=directory)
        await workspace.initialize()
        try:
            await equip_resources(catalog, workspace, "agent", "first", definition)
            first = (await workspace.list_mcps(agent_id="agent", session_id="first"))[0]
            await equip_resources(catalog, workspace, "agent", "first", definition)
            assert (await workspace.list_mcps(agent_id="agent", session_id="first"))[
                0
            ] is first
            await equip_resources(catalog, workspace, "agent", "second", definition)
            second = (await workspace.list_mcps(agent_id="agent", session_id="second"))[
                0
            ]
            assert second is not first
            client.execution_timeout = 12
            await equip_resources(catalog, workspace, "agent", "first", definition)
            changed = (await workspace.list_mcps(agent_id="agent", session_id="first"))[
                0
            ]
            assert changed is not first and changed.execution_timeout == 12
            assert (await workspace.list_mcps(agent_id="agent", session_id="second"))[
                0
            ] is second
        finally:
            await workspace.close()

        workspace = LocalWorkspace(workdir=directory)
        await workspace.initialize()
        try:
            await equip_resources(catalog, workspace, "agent", "first", definition)
            restored = await workspace.list_mcps(agent_id="agent", session_id="first")
            assert len(restored) == 1 and restored[0].execution_timeout == 12
            # 按角色声明清除已不再引用的服务。
            definition.mcps = []
            await equip_resources(catalog, workspace, "agent", "first", definition)
            assert await workspace.list_mcps(agent_id="agent", session_id="first") == []
            assert (
                len(await workspace.list_mcps(agent_id="agent", session_id="second"))
                == 1
            )
        finally:
            await workspace.close()

    asyncio.run(scenario())


def test_concurrent_sandbox_skill_installation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """用本地后端执行沙箱的原生安装实现，验证两个会话首次安装同一 Skill。"""

    class SandboxSkillsWorkspace(LocalWorkspace):
        """使用 Docker 工作空间所继承的 Skill 实现。"""

        add_skill = WorkspaceBase.add_skill
        list_skills = WorkspaceBase.list_skills

    async def scenario() -> None:
        catalog = configured_catalog()
        definition = catalog.definitions.agents["procurement"]
        workspace = SandboxSkillsWorkspace(workdir=str(tmp_path / "workspace"))
        await workspace.initialize()
        try:
            native_list = workspace.list_skills
            assert await native_list(agent_id="agent") == []
            barrier = asyncio.Barrier(2)
            calls = 0

            async def list_skills(*, agent_id: str | None = None) -> list[Skill]:
                """让两个会话同时读到尚未安装的状态。"""
                nonlocal calls
                skills = await native_list(agent_id=agent_id)
                calls += 1
                if calls <= 2:
                    await barrier.wait()
                return skills

            monkeypatch.setattr(workspace, "list_skills", list_skills)
            async with asyncio.timeout(10):
                await asyncio.gather(
                    equip_resources(catalog, workspace, "agent", "first", definition),
                    equip_resources(catalog, workspace, "agent", "second", definition),
                )
            skills = await native_list(agent_id="agent")
            assert [skill.name for skill in skills] == ["item-query"]
            assert (
                await workspace.get_backend().read_file(
                    str(Path(skills[0].dir) / "references" / "fields.md")
                )
                == (
                    catalog.skills_dir / "item-query" / "references" / "fields.md"
                ).read_bytes()
            )
        finally:
            await workspace.close()

    asyncio.run(scenario())
