"""将已声明的 Agent 能力关联到工具工厂，并提供用户与 Agent 的身份映射。"""

import json
from collections.abc import Callable
from importlib import import_module
from inspect import iscoroutinefunction, signature
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from agentscope.tool import ToolBase

from app.config import agents, mcp
from app.errors.agent import AgentError
from app.runtime.context import RunContext

type ToolFactory = Callable[[RunContext], ToolBase]


class AgentCatalog:
    """保存 Agent 能力与工具工厂，并将用户与 Agent 配置映射为框架 ID。"""

    def __init__(
        self,
        *,
        definitions: agents.AgentDefinitions | None = None,
        mcps: mcp.MCPDefinitions | None = None,
    ) -> None:
        """取得已校验配置，导入工具工厂供运行时按会话创建工具。"""
        self.definitions = definitions if definitions is not None else agents.cfg
        self.mcps = mcps if mcps is not None else mcp.cfg
        self.definitions.validate_resources(mcp_names=self.mcps.servers.keys())
        self.factories: dict[str, ToolFactory] = {}
        for definition in self.definitions.agents.values():
            for path in definition.tool_factories:
                if path not in self.factories:
                    self.factories[path] = self._load_factory(path)

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
    def user_id(owner: tuple[str, str]) -> str:
        """按站点和用户名确定框架用户归属。"""
        return json.dumps(owner, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def agent_id(user_id: str, key: str) -> str:
        """为同一用户的不同角色生成稳定且互不相同的 Agent ID。"""
        return str(uuid5(NAMESPACE_URL, f"agent:{user_id}:{key}"))

    def definition(
        self, user_id: str, agent_id: str
    ) -> tuple[str, agents.AgentDefinition]:
        """按用户所属 Agent ID 查找已配置角色，拒绝未登记的动态角色。"""
        for key, definition in self.definitions.agents.items():
            if self.agent_id(user_id, key) == agent_id:
                return key, definition
        raise AgentError("当前角色未配置或无权访问。", 404)
