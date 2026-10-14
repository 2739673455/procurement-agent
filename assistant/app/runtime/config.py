"""Runtime 的模型配置、角色定义和引用校验。"""

from typing import Any, Self

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.credential import CredentialBase, CredentialFactory
from pydantic import BaseModel, Field, SerializeAsAny, model_validator


class ModelConfig(BaseModel):
    """提供商凭据、原生生成参数和模型运行设置。"""

    credential: SerializeAsAny[CredentialBase]  # 提供商原生凭据；type 决定模型类。
    model: str = Field(min_length=1)  # 提供商接口中的模型标识。
    client_kwargs: dict[str, Any]  # SDK 的 with_options 参数，如 timeout、max_retries。
    image_inputs: bool  # 图片输入开关，用于附件组装和模型消息格式校验。
    context_size: int = Field(gt=0)  # 上下文压缩使用的 token 预算，覆盖模型卡及默认值。
    params: SerializeAsAny[BaseModel]  # 与提供商模型对应的原生 Parameters 对象。

    @model_validator(mode="before")
    @classmethod
    def _parse_model(cls, value: dict[str, Any]) -> dict[str, Any]:
        """根据凭据类型解析提供商凭据及生成参数。"""
        credential = CredentialFactory.from_dict(value.get("credential", {}))
        model_cls = credential.get_chat_model_class()
        return {
            **value,
            "credential": credential,
            "params": model_cls.Parameters.model_validate(value.get("params")),
        }


class AgentDefinition(BaseModel):
    """角色名称、资源、模型引用及 AgentScope 推理配置。"""

    name: str = Field(min_length=1)  # Agent 名称。
    description: str = Field(min_length=1)  # 供团队邀请时选择角色的能力说明。
    prompt: str  # 提示词文件路径，相对于 AgentCatalog.base_dir。
    model: str = Field(min_length=1)  # models 中的模型配置名。
    builtin_tools: list[str]  # 启用的框架工作空间工具。
    tool_factories: list[str]  # 业务工具工厂路径，格式为 Python模块:函数名。
    mcps: list[str]  # AgentCatalog.mcps 中的服务配置名。
    skills: list[str]  # AgentCatalog.skills_dir 下的 Skill 目录名。
    members: list[str]  # 可通过 AgentInvite 邀请的角色配置名。
    context: ContextConfig  # 框架上下文配置。
    react: ReActConfig  # 框架推理循环配置。


class AgentDefinitions(BaseModel):
    """模型和角色配置集合，以及默认入口角色。"""

    default: str  # 默认入口 Agent 的配置名。
    agents: dict[str, AgentDefinition]  # Agent 配置名到定义的映射。
    models: dict[str, ModelConfig]  # 模型配置名到凭据、生成参数及运行设置的映射。

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
