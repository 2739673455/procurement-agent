"""在用户工作空间内，为每轮工具调用绑定当前会话的默认目录。"""

from copy import copy

from agentscope.middleware import MiddlewareBase
from agentscope.tool import BackendBase, Bash, Edit, Glob, Grep, Read, Write
from agentscope.workspace import WorkspaceBase

from app.errors.agent import AgentError


class SessionDirectoryBackend(BackendBase):
    """为工具提供会话默认目录，绝对路径和上级路径仍可访问同一工作空间。"""

    def __init__(self, backend: BackendBase, directory: str):
        """绑定执行后端和目录；不改变用户共享后端的工作目录。"""
        self.backend = backend
        self.directory = directory

    async def getcwd(self) -> str:
        """返回当前会话的默认目录，供文件工具解析相对路径。"""
        return self.directory

    async def exec_shell(self, command, *, cwd=None, timeout=None):
        """在指定目录或当前会话目录执行命令。"""
        return await self.backend.exec_shell(
            command,
            cwd=self.abspath(cwd or self.directory, cwd=self.directory),
            timeout=timeout,
        )

    async def read_file(self, path: str) -> bytes:
        """读取以会话目录为起点解析的文件，保留绝对路径访问。"""
        return await self.backend.read_file(self.abspath(path, cwd=self.directory))

    async def write_file(self, path: str, data: bytes) -> None:
        """写入以会话目录为起点解析的文件，保留绝对路径访问。"""
        await self.backend.write_file(self.abspath(path, cwd=self.directory), data)


class SessionDirectoryMiddleware(MiddlewareBase):
    """为本轮文件工具设置会话目录，并向模型说明共享工作空间的访问方式。"""

    def __init__(self, backend: SessionDirectoryBackend, workspace_root: str):
        """保存本轮目录后端和用户工作空间根路径。"""
        self.backend = backend
        self.workspace_root = workspace_root

    async def on_reply(self, agent, input_kwargs, next_handler):
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

    async def on_system_prompt(self, agent, current_prompt: str) -> str:
        """向模型提供默认工作目录及同一用户工作空间内的访问范围。"""
        return (
            current_prompt
            + f"\n当前会话的默认工作目录是 {self.backend.directory}。"
            + f"用户工作空间根目录是 {self.workspace_root}；"
            + "可以通过绝对路径或相对路径访问同一工作空间内的其他目录。"
        )


async def session_directory_middlewares(
    user_id: str, agent_id: str, session_id: str, workspace: WorkspaceBase
) -> list[MiddlewareBase]:
    """首次执行时创建会话目录，并为本轮框架工具生成目录绑定中间件。"""
    backend = workspace.get_backend()
    directory = backend.join_path(workspace.workdir, "sessions", session_id, "work")
    result = await backend.exec_shell(["mkdir", "-p", "--", directory])
    if result.exit_code != 0:
        raise AgentError("无法创建会话工作目录。", 503)
    return [
        SessionDirectoryMiddleware(
            SessionDirectoryBackend(backend, directory), workspace.workdir
        )
    ]
