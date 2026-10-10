"""将配置中的 Agent 定义与模型引用登记到框架原生存储。"""

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
from starlette.datastructures import State

from app.runtime.catalog import AgentCatalog
from app.runtime.models import ChatCredential


class AgentService:
    """按用户登记预定义 Agent 和模型配置引用，模型密钥保留在服务端。"""

    def __init__(self, runtime: State) -> None:
        """绑定框架运行资源与已校验的能力目录。"""
        self.runtime: State = runtime
        self.storage: StorageBase = runtime.storage
        self.catalog: AgentCatalog = runtime.catalog

    def identity(self, owner: tuple[str, str]) -> tuple[str, str]:
        """按用户归属及默认入口 Agent 取得稳定的框架标识。"""
        user_id = self.catalog.user_id(owner)
        return user_id, self.catalog.agent_id(user_id, self.catalog.definitions.default)

    @staticmethod
    def reference_session_id(user_id: str, key: str) -> str:
        """标识原生 AgentInvite 工具读取成员 Agent 模型和工作空间配置的参考会话。"""
        return str(uuid5(NAMESPACE_URL, f"agent-reference:{user_id}:{key}"))

    async def configure(self, user_id: str, key: str) -> ChatModelConfig:
        """保存 Agent 定义及模型配置引用，返回框架会话模型配置。"""
        from app.config.loader import ROOT_DIR

        definition = self.catalog.definitions.agents[key]
        model_key = definition.model
        credential = ChatCredential(
            id=str(uuid5(NAMESPACE_URL, f"model:{user_id}:{model_key}")),
            name=model_key,
            config_key=model_key,
        )
        await self.storage.upsert_credential(user_id, credential)
        invitable = any(
            key in role.members for role in self.catalog.definitions.agents.values()
        )
        identifier = self.catalog.agent_id(user_id, key)
        await self.storage.upsert_agent(
            user_id,
            AgentRecord(
                id=identifier,
                user_id=user_id,
                data=AgentData(
                    id=identifier,
                    name=definition.name,
                    system_prompt=(ROOT_DIR / definition.prompt).read_text(
                        encoding="utf-8"
                    ),
                    context_config=definition.context,
                    react_config=definition.react,
                    invite_config=InviteConfig(
                        invitable=invitable, invite_description=definition.description
                    ),
                ),
            ),
        )
        return ChatModelConfig(
            type=credential.type,
            credential_id=credential.id,
            model=credential.settings.model,
            parameters={},
        )

    async def configure_user(self, user_id: str) -> dict[str, ChatModelConfig]:
        """登记全部 Agent 及参考会话，让原生 AgentInvite 工具使用成员 Agent 自身的模型。"""
        models: dict[str, ChatModelConfig] = {}
        for key, definition in self.catalog.definitions.agents.items():
            model = models[key] = await self.configure(user_id, key)
            if not any(
                key in role.members for role in self.catalog.definitions.agents.values()
            ):
                continue
            identifier = self.reference_session_id(user_id, key)
            agent_id = self.catalog.agent_id(user_id, key)
            workspace_id = await self.runtime.workspace_manager.assign_workspace_id(
                user_id=user_id, agent_id=agent_id, session_id=identifier
            )
            await self.storage.upsert_session(
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
