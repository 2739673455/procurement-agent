"""关联 Agent 定义、工具工厂与调用方提供的资源目录。"""

from collections.abc import Callable, Mapping
from importlib import import_module
from inspect import iscoroutinefunction, signature
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from agentscope.mcp import MCPClient
from agentscope.skill import LocalSkillLoader
from agentscope.tool import ToolBase

from .config import AgentDefinition, AgentDefinitions
from .context import RunContext
from .errors import RunError

type ToolFactory = Callable[[RunContext], ToolBase]


class AgentCatalog:
    """保存 Agent 能力与工具工厂，并将用户与 Agent 配置映射为框架 ID。"""

    def __init__(
        self,
        *,
        definitions: AgentDefinitions,
        mcps: Mapping[str, MCPClient],
        base_dir: Path,
        skills_dir: Path,
    ) -> None:
        """取得已校验配置，导入工具工厂供运行时按会话创建工具。"""
        self.definitions = definitions
        self.mcps = mcps
        self.base_dir = base_dir.resolve()
        self.skills_dir = skills_dir.resolve()
        self._factories: dict[str, ToolFactory] = {}
        for key, definition in self.definitions.agents.items():
            if set(definition.builtin_tools) - {
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
            }:
                raise ValueError(f"Agent {key} 引用了未知工作空间工具")
            if set(definition.mcps) - self.mcps.keys():
                raise ValueError(f"Agent {key} 引用了未知 MCP 服务")
            if not (self.base_dir / definition.prompt).is_file():
                raise ValueError(f"Agent {key} 的提示词文件不存在")
            for name in definition.skills:
                if (
                    Path(name).name != name
                    or not (self.skills_dir / name / "SKILL.md").is_file()
                ):
                    raise ValueError(f"Agent {key} 的 Skill 路径无效")
            for path in definition.tool_factories:
                if path not in self._factories:
                    self._factories[path] = self._load_factory(path)

    def create_tools(
        self, definition: AgentDefinition, context: RunContext
    ) -> list[ToolBase]:
        """为当前角色创建绑定运行上下文的业务工具。"""
        return [self._factories[path](context) for path in definition.tool_factories]

    async def validate_skills(self) -> None:
        """通过框架 Skill 加载器检查所引用目录的元数据。"""
        for name in {
            name for role in self.definitions.agents.values() for name in role.skills
        }:
            skills = await LocalSkillLoader(str(self.skills_dir / name)).list_skills()
            if len(skills) != 1 or skills[0].name != name:
                raise ValueError(f"Skill {name} 的元数据无效，name 须与目录名一致")

    @staticmethod
    def _load_factory(path: str) -> ToolFactory:
        """启动时导入同步工厂，运行时传入调用方绑定的上下文。"""
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
    def agent_id(user_id: str, key: str) -> str:
        """为同一用户的不同角色生成稳定且互不相同的 Agent ID。"""
        return str(uuid5(NAMESPACE_URL, f"agent:{user_id}:{key}"))

    def definition(self, user_id: str, agent_id: str) -> tuple[str, AgentDefinition]:
        """按用户所属 Agent ID 查找已配置角色，拒绝未登记的动态角色。"""
        for key, definition in self.definitions.agents.items():
            if self.agent_id(user_id, key) == agent_id:
                return key, definition
        raise RunError("当前角色未配置或无权访问。", "not_found")
