"""从配置加载角色定义，并验证模型、工具、MCP、Skill 和成员引用。"""

import json
from collections.abc import Callable
from importlib import import_module
from inspect import iscoroutinefunction, signature
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import frontmatter
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.mcp import MCPClient
from agentscope.tool import ToolBase
from omegaconf import OmegaConf
from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from app.config.app_config import CONFIG_DIR, ROOT_DIR, ConfigModel
from app.errors.agent import AgentError
from app.runtime.context import RunContext

type ToolFactory = Callable[[RunContext], ToolBase]


class AgentDefinition(ConfigModel):
    """角色能力声明；上下文和推理参数使用框架原生配置。"""

    name: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")  # 框架中的角色名称。
    description: str = Field(min_length=1)  # 成员能力说明。
    prompt: str  # 相对于 Assistant 根目录的提示词文件。
    model: str | None  # 模型配置名；null 使用启用的模型。
    builtin_tools: list[str]  # 启用的框架工作空间工具。
    tool_factories: list[str]  # 工厂函数路径，格式为 Python模块:函数名。
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
    """从服务配置名生成客户端 name，其余参数使用框架原生结构。"""

    servers: dict[str, MCPClient]  # 服务配置名到原生 MCP 客户端配置的映射。

    @model_validator(mode="before")
    @classmethod
    def assign_client_names(cls, data):
        """在框架校验前补入 name，配置文件只声明外层服务名。"""
        if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
            return data
        servers = {}
        for name, config in data["servers"].items():
            if not isinstance(config, dict):
                raise PydanticCustomError(
                    "mcp_config_type", "MCP 服务必须使用配置字典声明"
                )
            if "name" in config:
                raise ValueError("MCP name 由外层服务配置名生成，无需填写")
            servers[name] = {**config, "name": name}
        return {**data, "servers": servers}


def load_yaml(path: Path):
    """展开配置中的环境变量插值，保留实际文件位置。"""
    return OmegaConf.to_container(OmegaConf.load(path), resolve=True)


class AgentCatalog:
    """验证角色能力，并将用户与角色映射为稳定的框架 Agent ID。"""

    def __init__(self, *, definitions=None, mcps=None):
        """加载配置，验证业务能力和资源引用后供服务层装配。"""
        from app.config import app_config

        self.definitions = definitions or AgentDefinitions.model_validate(
            load_yaml(CONFIG_DIR / "agents.yaml")
        )
        self.mcps = mcps or MCPDefinitions.model_validate(
            load_yaml(CONFIG_DIR / "mcp.yaml")
        )
        self.factories: dict[str, ToolFactory] = {}
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
            for path in definition.tool_factories:
                if path not in self.factories:
                    self.factories[path] = self._load_factory(path)
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
    def _load_factory(path: str) -> ToolFactory:
        """启动时导入同步工厂并校验调用签名，运行时传入认证上下文。"""
        module, separator, name = path.partition(":")
        if (
            not separator
            or not name.isidentifier()
            or not all(part.isidentifier() for part in module.split("."))
        ):
            raise ValueError(f"工具工厂路径须为 Python模块:函数名：{path}")
        try:
            factory = getattr(import_module(module), name)
        except (ImportError, AttributeError) as exc:
            raise ValueError(f"无法加载工具工厂：{path}") from exc
        if not callable(factory) or iscoroutinefunction(factory):
            raise ValueError(f"工具工厂必须是接收运行上下文的同步函数：{path}")
        try:
            signature(factory).bind(None)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"工具工厂须接收一个运行上下文参数：{path}") from exc
        return cast(ToolFactory, factory)

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
