"""定义、加载和校验 Agent 配置及其模型、工具、MCP 和 Skill 引用。"""

from collections.abc import Collection
from pathlib import Path
from typing import Any, Self

import frontmatter
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.credential import CredentialBase, CredentialFactory
from pydantic import BaseModel, Field, SerializeAsAny, field_validator, model_validator

from app.config.loader import CONFIG_DIR, ROOT_DIR, load_yaml


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
    def parse_credential(cls, value: dict[str, Any]) -> CredentialBase:
        """由框架凭据工厂解析提供商配置，并保护密钥字段。"""
        return CredentialFactory.from_dict(value)

    @model_validator(mode="after")
    def validate_parameters(self) -> Self:
        """启动时按原生模型参数类型校验。"""
        model_cls = self.credential.get_chat_model_class()
        model_cls.Parameters.model_validate(self.params)
        return self


class AgentDefinition(BaseModel):
    """Agent 能力声明；上下文和推理参数使用框架原生配置。"""

    name: str = Field(min_length=1)  # Agent 名称。
    description: str = Field(min_length=1)  # Agent 能力说明，供协作时选用。
    prompt: str  # 相对于 Assistant 根目录的提示词文件。
    model: str = Field(min_length=1)  # models 中的模型配置名。
    builtin_tools: list[str]  # 启用的框架工作空间工具。
    tool_factories: list[str]  # 工厂函数路径，格式为 Python模块:函数名。
    mcps: list[str]  # mcp.yaml 中的服务名称。
    skills: list[str]  # resources/skills 下的 Skill 目录名称。
    members: list[str]  # 允许邀请加入团队的其他 Agent 的配置名。
    context: ContextConfig  # 框架上下文配置。
    react: ReActConfig  # 框架推理循环配置。


class AgentDefinitions(BaseModel):
    """预定义 Agent、可用模型与默认入口。"""

    default: str  # 默认入口 Agent 的配置名。
    agents: dict[str, AgentDefinition]  # Agent 配置名到定义的映射。
    models: dict[str, ModelConfig]  # 模型配置名到连接参数和能力声明的映射。

    @model_validator(mode="after")
    def validate_references(self) -> Self:
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

    def validate_resources(self, *, mcp_names: Collection[str]) -> None:
        """校验 MCP、内置工具、提示词文件和 Skill 元数据。"""
        for key, definition in self.agents.items():
            if set(definition.builtin_tools) - {
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
            }:
                raise ValueError(f"Agent {key} 引用了未知工作空间工具")
            if set(definition.mcps) - set(mcp_names):
                raise ValueError(f"Agent {key} 引用了未知 MCP 服务")
            if not (ROOT_DIR / definition.prompt).is_file():
                raise ValueError(f"Agent {key} 的提示词文件不存在")
            for skill in definition.skills:
                if (
                    Path(skill).name != skill
                    or not (
                        ROOT_DIR / "resources" / "skills" / skill / "SKILL.md"
                    ).is_file()
                ):
                    raise ValueError(f"Agent {key} 的 Skill 路径无效")
                metadata = frontmatter.load(
                    ROOT_DIR / "resources" / "skills" / skill / "SKILL.md"
                )
                if metadata.get("name") != skill or not metadata.get("description"):
                    raise ValueError(f"Agent {key} 的 Skill 元数据无效")


cfg = AgentDefinitions.model_validate(load_yaml(CONFIG_DIR / "agents.yaml"))
