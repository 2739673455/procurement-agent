"""将预定义角色、提供商凭据和模型参数登记到框架原生存储。"""

from uuid import NAMESPACE_URL, uuid5

from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    ChatModelConfig,
    SessionConfig,
    SessionNaming,
    StorageBase,
)
from agentscope.app.storage._model._agent import InviteConfig
from agentscope.app.workspace_manager import WorkspaceManagerBase
from starlette.datastructures import State

from .catalog import AgentCatalog


class AgentRegistry:
    """创建用户会话时初始化原生 Agent、凭据和成员配置会话。"""

    def __init__(self, runtime: State) -> None:
        """绑定框架运行资源与已校验的能力目录。"""
        self._storage: StorageBase = runtime.storage
        self._catalog: AgentCatalog = runtime.catalog
        self._workspace_manager: WorkspaceManagerBase = runtime.workspace_manager

    @staticmethod
    def reference_session_id(user_id: str, key: str) -> str:
        """生成角色参考会话 ID，供 AgentInvite 读取成员的模型和工作空间配置。"""
        return str(uuid5(NAMESPACE_URL, f"agent-reference:{user_id}:{key}"))

    async def configure_user(self, user_id: str) -> dict[str, ChatModelConfig]:
        """登记原生凭据与角色，让 AgentInvite 从成员配置会话继承模型。"""
        configured: dict[str, ChatModelConfig] = {}
        for key, settings in self._catalog.definitions.models.items():
            credential = settings.credential.model_copy(
                update={
                    "id": str(uuid5(NAMESPACE_URL, f"model:{user_id}:{key}")),
                    "name": key,
                }
            )
            await self._storage.upsert_credential(user_id, credential)
            configured[key] = ChatModelConfig(
                type=credential.model_dump()["type"],
                credential_id=credential.id,
                model=settings.model,
                parameters=settings.params.model_dump(mode="json", exclude_none=True),
            )
        invitable = {
            key
            for definition in self._catalog.definitions.agents.values()
            for key in definition.members
        }
        models: dict[str, ChatModelConfig] = {}
        for key, definition in self._catalog.definitions.agents.items():
            model = models[key] = configured[definition.model]
            agent_id = self._catalog.agent_id(user_id, key)
            await self._storage.upsert_agent(
                user_id,
                AgentRecord(
                    id=agent_id,
                    user_id=user_id,
                    data=AgentData(
                        id=agent_id,
                        name=definition.name,
                        system_prompt=(
                            self._catalog.base_dir / definition.prompt
                        ).read_text(encoding="utf-8"),
                        context_config=definition.context,
                        react_config=definition.react,
                        invite_config=InviteConfig(
                            invitable=key in invitable,
                            invite_description=definition.description,
                        ),
                    ),
                ),
            )
            if key not in invitable:
                continue
            identifier = self.reference_session_id(user_id, key)
            workspace_id = await self._workspace_manager.assign_workspace_id(
                user_id=user_id, agent_id=agent_id, session_id=identifier
            )
            await self._storage.upsert_session(
                user_id,
                agent_id,
                SessionConfig(
                    workspace_id=workspace_id,
                    name=definition.description,
                    naming=SessionNaming(auto=False),
                    chat_model_config=model,
                ),
                session_id=identifier,
            )
        return models
