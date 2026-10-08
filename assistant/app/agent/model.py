"""向 AgentScope 注册模型配置引用；数据库仅保存引用，密钥由服务端解析。"""

from typing import Any, Literal

from agentscope.credential import CredentialBase, OpenAICredential

from app.config import app_config
from app.errors.agent import AgentError


class ChatCredential(CredentialBase):
    """引用服务端配置，供框架装配 Chat Completions 模型。"""

    type: Literal["procurement_chat"] = "procurement_chat"
    config_key: str

    @property
    def settings(self):
        settings = app_config.cfg.lm_config.models.get(self.config_key)
        # 持久化的引用可能指向已从服务端配置中删除的模型。
        if settings is None:
            raise AgentError("请检查配置文件中的模型配置。", 503)
        return settings

    @classmethod
    def get_chat_model_class(cls):
        from app.clients.chat_model import ChatCompletionsModel

        return ChatCompletionsModel


def connection_options(reference: ChatCredential) -> dict[str, Any]:
    settings = reference.settings
    return {
        "credential": OpenAICredential(
            api_key=settings.api_key,
            base_url=settings.base_url,
        ),
        "model": settings.model,
        "stream": True,
        "context_size": settings.profile.max_input_tokens or 32768,
        "max_retries": 0,
        "client_kwargs": {"timeout": settings.timeout_seconds, "max_retries": 0},
    }
