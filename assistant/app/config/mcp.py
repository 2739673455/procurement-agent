"""加载 MCP 服务配置。"""

from agentscope.mcp import MCPClient
from pydantic import BaseModel, model_validator

from app.config.loader import CONFIG_DIR, load_yaml


class MCPDefinitions(BaseModel):
    """MCP 服务配置。"""

    servers: dict[str, MCPClient]  # 服务名与客户端配置。

    @model_validator(mode="before")
    @classmethod
    def assign_client_names(cls, data: object) -> object:
        """使用服务名作为客户端名称。"""
        if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
            return data
        servers = {}
        for name, config in data["servers"].items():
            servers[name] = {**config, "name": name}
        return {**data, "servers": servers}


cfg = MCPDefinitions.model_validate(load_yaml(CONFIG_DIR / "mcp.yaml"))
