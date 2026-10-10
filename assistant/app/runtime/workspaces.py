"""在用户工作空间内，为每轮工具调用绑定当前会话的默认目录。"""

from collections.abc import AsyncGenerator, Callable
from copy import copy
from typing import Any

from agentscope.agent import Agent
from agentscope.event import AgentEvent
from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase
from agentscope.tool import BackendBase, Bash, Edit, ExecResult, Glob, Grep, Read, Write
from agentscope.workspace import WorkspaceBase

from app.errors.agent import AgentError


class SessionDirectoryBackend(BackendBase):
    """绑定会话执行目录，文件路径按框架原生约定传给工作空间后端。"""

    def __init__(self, backend: BackendBase, directory: str) -> None:
        """绑定执行后端和目录；不改变用户共享后端的工作目录。"""
        self.backend = backend
        self.directory = directory

    async def getcwd(self) -> str:
        """返回会话目录，供 Bash、Glob、Grep 确定默认执行或搜索位置。"""
        return self.directory

    async def exec_shell(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """在指定目录或当前会话目录执行命令。"""
        return await self.backend.exec_shell(
            command,
            cwd=self.abspath(cwd or self.directory, cwd=self.directory),
            timeout=timeout,
        )

    async def read_file(self, path: str) -> bytes:
        """读取原生文件工具传入的绝对路径。"""
        return await self.backend.read_file(path)

    async def write_file(self, path: str, data: bytes) -> None:
        """写入原生文件工具传入的绝对路径。"""
        await self.backend.write_file(path, data)


class SessionDirectoryMiddleware(MiddlewareBase):
    """为本轮文件工具设置会话目录，并向模型说明共享工作空间的访问方式。"""

    def __init__(self, backend: SessionDirectoryBackend, workspace_root: str) -> None:
        """保存本轮目录后端和用户工作空间根路径。"""
        self.backend = backend
        self.workspace_root = workspace_root

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[AgentEvent | Msg]],
    ) -> AsyncGenerator[AgentEvent | Msg]:
        """绑定本轮工具副本，保留框架工具配置及其他工具组。"""
        for group in agent.toolkit.tool_groups:
            for index, tool in enumerate(group.tools):
                if isinstance(tool, (Bash, Edit, Glob, Grep, Read, Write)):
                    # 框架工具用实例字段保存后端；仅调整本轮副本，不修改共享对象。
                    scoped = copy(tool)
                    scoped._backend = self.backend
                    if isinstance(scoped, Bash):
                        scoped._cwd = self.backend.directory
                    group.tools[index] = scoped
        async for event in next_handler(**input_kwargs):
            yield event

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        """向模型提供默认工作目录及同一用户工作空间内的访问范围。"""
        return (
            current_prompt
            + f"\n当前会话的默认工作目录是 {self.backend.directory}。"
            + "Bash 中的相对路径以当前会话目录为起点。"
            + "Read、Write、Edit 的 file_path 必须使用绝对路径；"
            + "访问当前会话的文件时，请以当前会话目录拼接完整路径。"
            + "Glob、Grep 默认搜索当前会话目录。"
            + f"用户工作空间根目录是 {self.workspace_root}，"
            + "可以访问同一用户工作空间内其他会话的目录。"
        )


def session_directory(workspace: WorkspaceBase, session_id: str) -> str:
    """返回当前会话目录，统一工具工作目录和附件保存位置。"""
    return workspace.get_backend().join_path(workspace.workdir, "sessions", session_id)


async def session_directory_middlewares(
    user_id: str, agent_id: str, session_id: str, workspace: WorkspaceBase
) -> list[MiddlewareBase]:
    """首次执行时创建会话目录，并为本轮框架工具生成目录绑定中间件。"""
    backend = workspace.get_backend()
    directory = session_directory(workspace, session_id)
    result = await backend.exec_shell(["mkdir", "-p", "--", directory])
    if result.exit_code != 0:
        raise AgentError("无法创建会话工作目录。", 503)
    return [
        SessionDirectoryMiddleware(
            SessionDirectoryBackend(backend, directory), workspace.workdir
        )
    ]
