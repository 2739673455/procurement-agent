"""定义、加载和校验应用配置。"""

from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from app.config.loader import CONFIG_DIR, load_yaml


class LogConfig(BaseModel):
    """日志级别和滚动配置。"""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]  # 日志级别。
    rotation: str  # Loguru 日志滚动条件，例如 "10 MB"。


class ServerConfig(BaseModel):
    """Assistant HTTP 服务的监听地址。"""

    host: str  # 监听主机；空字符串表示自动检测 Docker 网桥网关。
    port: int = Field(ge=1, le=65535)  # HTTP 监听端口。


class PostgresConfig(BaseModel):
    """AgentScope 存储会话、消息和状态所用的 PostgreSQL 连接。"""

    host: str = Field(min_length=1)  # 数据库主机地址。
    port: int = Field(ge=1, le=65535)  # 数据库连接端口。
    user: str = Field(min_length=1)  # 数据库用户名。
    password: SecretStr = Field(min_length=1)  # 数据库密码，由环境变量提供。
    database: str = Field(min_length=1)  # 数据库名称。


class ERPNextConfig(BaseModel):
    """ERPNext 身份验证和业务查询的连接配置。"""

    base_url: str = Field(min_length=1)  # ERPNext HTTP 接口根地址。
    site: str = Field(min_length=1)  # Frappe 站点名，用于请求 Host 和用户归属隔离。
    timeout_seconds: float = Field(gt=0)  # 请求超时，单位为秒，必须大于零。


class DockerWorkspaceConfig(BaseModel):
    """AgentScope Docker 工作空间的镜像与空闲回收配置。"""

    base_image: str = Field(min_length=1)  # 基础镜像，须提供 python3。
    node_version: str = Field(min_length=1)  # 镜像内 Node.js 的主版本。
    extra_pip: list[str]  # 安装到容器内的额外 Python 包，无需额外包时填写 []。
    ttl_seconds: float = Field(gt=0)  # 工作空间空闲回收时长，单位为秒。
    sweep_interval_seconds: float = Field(gt=0)  # 空闲回收扫描间隔，单位为秒。


class RuntimeConfig(BaseModel):
    """所有角色共用的提示词时区及任务登录上下文有效期。"""

    timezone: str = Field(min_length=1)  # 提示词注入使用的 IANA 时区。
    context_ttl_seconds: float = Field(gt=0)  # 服务端内存中的任务登录上下文有效期。


class AppConfig(BaseModel):
    """应用配置根结构，与 YAML 的顶层分组对应。"""

    log: LogConfig  # 日志输出配置。
    server: ServerConfig  # HTTP 服务配置。
    postgresql: PostgresConfig  # AgentScope 持久化连接配置。
    erpnext: ERPNextConfig  # ERPNext 连接与站点配置。
    workspace: DockerWorkspaceConfig  # 按用户分配的 Docker 执行工作空间。
    runtime: RuntimeConfig  # 所有 Agent 共用的运行配置。


cfg = AppConfig.model_validate(load_yaml(CONFIG_DIR / "app.yaml"))
