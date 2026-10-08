"""将服务端模型配置注入 AgentScope Chat Completions 模型。"""

from agentscope.formatter import DeepSeekChatFormatter
from agentscope.model import OpenAIChatModel

from app.agent.model import ChatCredential, connection_options


class ChatCompletionsModel(OpenAIChatModel):
    def __init__(self, credential: ChatCredential, model: str, parameters=None):
        settings = credential.settings
        formatter = None
        if settings.model_provider == "deepseek":
            formatter = DeepSeekChatFormatter(
                input_types=["text/plain", "image/*"]
                if settings.profile.image_inputs
                else ["text/plain"]
            )
        super().__init__(
            **connection_options(credential),
            extra_body=settings.params,
            formatter=formatter,
        )
