"""通过框架工作空间保存附件，按当前会话的文件名读取。"""

import base64
import binascii
import mimetypes

from app.errors.agent import AgentError
from app.runtime.workspaces import session_directory

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


class AttachmentService:
    """管理会话附件，同名上传覆盖，文件生命周期随会话目录。"""

    def __init__(self, workspace_manager):
        """绑定与 Agent 执行相同的工作空间管理器。"""
        self.workspace_manager = workspace_manager

    async def _workspace(self, user_id, row):
        """根据已认证会话获取用户工作空间。"""
        return await self.workspace_manager.get_workspace(
            user_id, row.agent_id, row.id, row.config.workspace_id
        )

    @staticmethod
    def _name(name: str) -> str:
        """附件名称只能表示一个文件，不允许空名称、路径或特殊目录名。"""
        if (
            not name
            or len(name) > 255
            or name in {".", ".."}
            or any(c in name for c in ("/", "\\", "\0"))
        ):
            raise AgentError("附件名称无效，不能包含路径。")
        return name

    @staticmethod
    def _metadata(backend, directory, name):
        """根据文件名生成类型和路径，消息元数据使用同一来源。"""
        return {
            "name": name,
            "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "path": backend.join_path(directory, name),
        }

    async def save(self, user_id, row, upload):
        """把原始内容写入会话附件目录，同名文件直接覆盖。"""
        name = self._name(upload.name)
        try:
            content = base64.b64decode(upload.data, validate=True)
        except (ValueError, binascii.Error):
            raise AgentError("附件内容无效。") from None
        workspace = await self._workspace(user_id, row)
        backend = workspace.get_backend()
        directory = backend.join_path(
            session_directory(workspace, row.id), "attachments"
        )
        result = await backend.exec_shell(["mkdir", "-p", "--", directory])
        if result.exit_code:
            raise AgentError("无法创建附件目录。", 503)
        metadata = self._metadata(backend, directory, name)
        await backend.write_file(metadata["path"], content)
        return metadata

    async def load(self, user_id, row, names):
        """读取当前会话中的文件，仅将图片原始内容补入模型输入。"""
        if not names:
            return []
        names = list(dict.fromkeys(self._name(name) for name in names))
        workspace = await self._workspace(user_id, row)
        backend = workspace.get_backend()
        directory = backend.join_path(
            session_directory(workspace, row.id), "attachments"
        )
        files = []
        for name in names:
            item = self._metadata(backend, directory, name)
            exists = await backend.exec_shell(["test", "-f", item["path"]])
            if exists.exit_code:
                raise AgentError(f"附件 {name} 不存在于当前会话。", 404)
            if item["media_type"] in IMAGE_TYPES:
                item["data"] = base64.b64encode(
                    await backend.read_file(item["path"])
                ).decode()
            files.append(item)
        return files
