"""Agent 能力与模型配置；解析和引用校验不依赖配置文件格式。"""

from typing import Any, Self

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.credential import CredentialBase, CredentialFactory
from pydantic import BaseModel, Field, SerializeAsAny, field_validator, model_validator


class ModelConfig(BaseModel):
    """单个模型的服务商、连接参数和能力声明。"""

    credential: SerializeAsAny[CredentialBase]  # 原生凭据；type 决定模型类和消息格式。
    model: str = Field(min_length=1)  # 服务商的模型标识，不可为空。
    client_kwargs: dict[str, Any]  # 传给提供商 SDK 的连接选项，例如 timeout。
    image_inputs: bool  # 是否允许向模型发送图片附件。
    context_size: int | None = Field(gt=0)  # 上下文 token 预算；null 时使用 32768。
    params: dict[str, Any]  # 提供商原生 Parameters 字段，无附加参数时填写 {}。

    @field_validator("credential", mode="before")
    @classmethod
    def _parse_credential(
        cls, value: CredentialBase | dict[str, Any]
    ) -> CredentialBase:
        """由框架凭据工厂解析提供商配置，并保护密钥字段。"""
        if isinstance(value, CredentialBase):
            return value
        return CredentialFactory.from_dict(value)

    @model_validator(mode="after")
    def _validate_parameters(self) -> Self:
        """启动时按原生模型参数类型校验。"""
        model_cls = self.credential.get_chat_model_class()
        model_cls.Parameters.model_validate(self.params)
        return self


class AgentDefinition(BaseModel):
    """Agent 能力声明；上下文和推理参数使用框架原生配置。"""

    name: str = Field(min_length=1)  # Agent 名称。
    description: str = Field(min_length=1)  # Agent 能力说明，供协作时选用。
    prompt: str  # 相对于资源基目录的提示词文件。
    model: str = Field(min_length=1)  # models 中的模型配置名。
    builtin_tools: list[str]  # 启用的框架工作空间工具。
    tool_factories: list[str]  # 工厂函数路径，格式为 Python模块:函数名。
    mcps: list[str]  # MCP 客户端配置名。
    skills: list[str]  # Skill 根目录下的目录名称。
    members: list[str]  # 允许邀请加入团队的其他 Agent 的配置名。
    context: ContextConfig  # 框架上下文配置。
    react: ReActConfig  # 框架推理循环配置。


class AgentDefinitions(BaseModel):
    """预定义 Agent、可用模型与默认入口。"""

    default: str  # 默认入口 Agent 的配置名。
    agents: dict[str, AgentDefinition]  # Agent 配置名到定义的映射。
    models: dict[str, ModelConfig]  # 模型配置名到连接参数和能力声明的映射。

    @model_validator(mode="after")
    def _validate_references(self) -> Self:
        """校验默认入口、名称、模型和协作 Agent 引用，拒绝 Agent 邀请自身。"""
        if self.default not in self.agents:
            raise ValueError("default 必须引用已声明的 Agent")
        names = [definition.name for definition in self.agents.values()]
        if len(names) != len(set(names)):
            raise ValueError("Agent 名称不能重复")
        for key, definition in self.agents.items():
            if definition.model not in self.models:
                raise ValueError(f"Agent {key} 引用了未知模型：{definition.model}")
            if (
                key in definition.members
                or set(definition.members) - self.agents.keys()
            ):
                raise ValueError(f"Agent {key} 的协作 Agent 引用无效")
        return self
