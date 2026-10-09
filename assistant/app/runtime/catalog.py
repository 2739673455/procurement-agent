"""从配置加载角色定义，并验证模型、工具、MCP、Skill 和成员引用。"""

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import frontmatter
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.mcp import MCPClient
from omegaconf import OmegaConf
from pydantic import Field, model_validator

from app.config.app_config import CONFIG_DIR, ROOT_DIR, ConfigModel
from app.errors.agent import AgentError


class AgentDefinition(ConfigModel):
    """角色能力声明；上下文和推理参数使用框架原生配置。"""

    name: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")  # 框架中的角色名称。
    description: str = Field(min_length=1)  # 成员能力说明。
    prompt: str  # 相对于 Assistant 根目录的提示词文件。
    model: str | None  # 模型配置名；null 使用启用的模型。
    builtin_tools: list[str]  # 启用的框架工作空间工具。
    tool_factories: list[str]  # 业务工具工厂名称。
    mcps: list[str]  # mcp.yaml 中的服务名称。
    skills: list[str]  # resources/skills 下的 Skill 目录名称。
    members: list[str]  # 允许邀请的角色配置名。
    tool_offload: bool  # 是否允许框架将超时工具转为后台任务。
    context: ContextConfig  # 框架上下文配置。
    react: ReActConfig  # 框架推理循环配置。


class AgentDefinitions(ConfigModel):
    """可用角色集合与默认入口角色。"""

    default: str  # 默认入口角色的配置名。
    agents: dict[str, AgentDefinition]  # 角色配置名到定义的映射。

    @model_validator(mode="after")
    def validate_references(self):
        """校验入口、角色名称以及成员引用，拒绝自我邀请。"""
        if self.default not in self.agents:
            raise ValueError("default 必须引用已声明的角色")
        names = [definition.name for definition in self.agents.values()]
        if len(names) != len(set(names)):
            raise ValueError("角色名称不能重复")
        for key, definition in self.agents.items():
            if (
                key in definition.members
                or set(definition.members) - self.agents.keys()
            ):
                raise ValueError(f"角色 {key} 的成员引用无效")
        return self


class MCPDefinitions(ConfigModel):
    """MCP 连接配置，协议参数直接使用框架模型。"""

    servers: dict[str, MCPClient]  # 服务配置名到原生 MCP 客户端配置的映射。


def load_yaml(path: Path):
    """展开配置中的环境变量插值，保留实际文件位置。"""
    return OmegaConf.to_container(OmegaConf.load(path), resolve=True)


class AgentCatalog:
    """验证角色能力，并将用户与角色映射为稳定的框架 Agent ID。"""

    def __init__(self, factories, *, definitions=None, mcps=None):
        """加载配置，验证业务能力和资源引用后供服务层装配。"""
        from app.config import app_config

        self.definitions = definitions or AgentDefinitions.model_validate(
            load_yaml(CONFIG_DIR / "agents.yaml")
        )
        self.mcps = mcps or MCPDefinitions.model_validate(
            load_yaml(CONFIG_DIR / "mcp.yaml")
        )
        for name, client in self.mcps.servers.items():
            if client.name != name:
                raise ValueError("MCP 配置名必须与客户端 name 一致")
        self.factories = factories
        for key, definition in self.definitions.agents.items():
            if set(definition.builtin_tools) - {
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
            }:
                raise ValueError(f"角色 {key} 引用了未知工作空间工具")
            if set(definition.tool_factories) - factories.keys():
                raise ValueError(f"角色 {key} 引用了未知工具工厂")
            if set(definition.mcps) - self.mcps.servers.keys():
                raise ValueError(f"角色 {key} 引用了未知 MCP 服务")
            if (
                definition.model is not None
                and definition.model not in app_config.cfg.lm_config.models
            ):
                raise ValueError(f"角色 {key} 引用了未知模型")
            if not (ROOT_DIR / definition.prompt).is_file():
                raise ValueError(f"角色 {key} 的提示词文件不存在")
            for skill in definition.skills:
                if (
                    Path(skill).name != skill
                    or not (
                        ROOT_DIR / "resources" / "skills" / skill / "SKILL.md"
                    ).is_file()
                ):
                    raise ValueError(f"角色 {key} 的 Skill 路径无效")
                metadata = frontmatter.load(
                    ROOT_DIR / "resources" / "skills" / skill / "SKILL.md"
                )
                if metadata.get("name") != skill or not metadata.get("description"):
                    raise ValueError(f"角色 {key} 的 Skill 元数据无效")

    @staticmethod
    def user_id(owner) -> str:
        """按站点和用户名确定框架用户归属。"""
        return json.dumps(owner, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def agent_id(user_id: str, key: str) -> str:
        """为同一用户的不同角色生成稳定且互不相同的 Agent ID。"""
        return str(uuid5(NAMESPACE_URL, f"agent:{user_id}:{key}"))

    def definition(self, user_id: str, agent_id: str) -> tuple[str, AgentDefinition]:
        """按用户所属 Agent ID 查找已配置角色，拒绝未登记的动态角色。"""
        for key, definition in self.definitions.agents.items():
            if self.agent_id(user_id, key) == agent_id:
                return key, definition
        raise AgentError("当前角色未配置或无权访问。", 404)
