"""AgentScope 模型装配；数据库保存配置引用，连接参数由服务端解析。"""

from typing import Literal

from agentscope.credential import CredentialBase, OpenAICredential
from agentscope.formatter import DeepSeekChatFormatter
from agentscope.model import OpenAIChatModel

from app.config import app_config
from app.errors.agent import AgentError


class ChatCredential(CredentialBase):
    """引用服务端配置，供框架装配 Chat Completions 模型。"""

    # 框架通过此类型标识识别和还原凭据。
    type: Literal["procurement_chat"] = "procurement_chat"
    config_key: str  # 服务端模型配置集合中的条目名称。

    @property
    def settings(self):
        """按配置引用读取模型设置，引用不存在时抛出配置错误。"""
        settings = app_config.cfg.lm_config.models.get(self.config_key)
        # 数据库中的配置引用必须对应服务端模型配置中的条目。
        if settings is None:
            raise AgentError("请检查配置文件中的模型配置。", 503)
        return settings

    @classmethod
    def get_chat_model_class(cls):
        """向框架凭据工厂提供要装配的模型类。"""
        return ChatCompletionsModel


class ChatCompletionsModel(OpenAIChatModel):
    """将服务端配置和服务商消息格式注入框架模型。"""

    def __init__(self, credential: ChatCredential, model: str, parameters=None):
        """从凭据引用解析连接参数，并配置服务商消息格式与请求参数。"""
        settings = credential.settings
        formatter = None
        if settings.model_provider == "deepseek":
            formatter = DeepSeekChatFormatter(
                input_types=["text/plain", "image/*"]
                if settings.image_inputs
                else ["text/plain"]
            )
        super().__init__(
            credential=OpenAICredential(
                api_key=settings.api_key,
                base_url=settings.base_url,
            ),
            model=settings.model,
            context_size=settings.context_size or 128000,
            max_retries=0,
            client_kwargs={"timeout": settings.timeout_seconds, "max_retries": 0},
            extra_body=settings.params,
            formatter=formatter,
        )
