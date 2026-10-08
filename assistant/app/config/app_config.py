"""通过 dotenv 和 OmegaConf 加载环境变量与 YAML 配置，并校验配置类型。"""

from pathlib import Path
from typing import Any, Literal, Self

from dotenv import load_dotenv
from omegaconf import OmegaConf
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

ROOT_DIR = Path(__file__).resolve().parents[2]  # Assistant 服务根目录。
CONFIG_DIR = ROOT_DIR / "conf"  # YAML 配置和 .env 所在目录。
CONFIG_FILE = CONFIG_DIR / "app_config.yaml"  # 应用配置文件路径。


class ConfigModel(BaseModel):
    """配置字段须显式提供，拒绝未知字段，校验错误不展示输入值。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LogConfig(ConfigModel):
    """日志级别和滚动配置。"""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]  # 日志级别。
    rotation: str  # Loguru 日志滚动条件，例如 "10 MB"。


class ServerConfig(ConfigModel):
    """Assistant HTTP 服务的监听地址。"""

    host: str  # 监听主机；空字符串表示自动检测 Docker 网桥网关。
    port: int = Field(ge=1, le=65535)  # HTTP 监听端口。


class PostgresConfig(ConfigModel):
    """AgentScope 存储会话、消息和状态所用的 PostgreSQL 连接。"""

    host: str = Field(min_length=1)  # 数据库主机地址。
    port: int = Field(ge=1, le=65535)  # 数据库连接端口。
    user: str = Field(min_length=1)  # 数据库用户名。
    password: SecretStr = Field(min_length=1)  # 数据库密码，由环境变量提供。
    database: str = Field(min_length=1)  # 数据库名称。


class ERPNextConfig(ConfigModel):
    """ERPNext 身份验证和业务查询的连接配置。"""

    base_url: str = Field(min_length=1)  # ERPNext HTTP 接口根地址。
    site: str = Field(min_length=1)  # Frappe 站点名，用于请求 Host 和用户归属隔离。
    timeout_seconds: float = Field(gt=0)  # 请求超时，单位为秒，必须大于零。


class ModelProfileConfig(ConfigModel):
    """声明模型能力，未知的上下文大小显式填写 null。"""

    image_inputs: bool  # 是否允许向模型发送图片附件。
    max_input_tokens: int | None = Field(gt=0)  # 上下文 token 预算；null 时使用 32768。


class ModelConfig(ConfigModel):
    """单个模型的服务商、连接参数和能力声明。"""

    model_provider: str = Field(min_length=1)  # 服务商标识，决定消息格式化方式。
    profile: ModelProfileConfig  # 图片输入和上下文预算的能力声明。
    model: str = Field(min_length=1)  # 服务商的模型标识，不可为空。
    base_url: str = Field(min_length=1)  # 模型接口根地址，不可为空。
    api_key: SecretStr = Field(min_length=1)  # 从环境变量读取的模型密钥，不可为空。
    timeout_seconds: float = Field(gt=0)  # 模型请求超时，单位为秒。
    params: dict[str, Any]  # 透传到模型请求体的附加参数，无附加参数时填写 {}。

    @model_validator(mode="after")
    def validate_params(self) -> Self:
        """禁止附加参数覆盖连接配置和框架管理的请求字段。"""

        reserved = {
            "model",
            "model_name",
            "model_provider",
            "profile",
            "base_url",
            "api_key",
            "timeout",
            "stream",
            "messages",
            "tools",
            "tool_choice",
            "store",
        }
        conflicts = reserved & self.params.keys()
        if conflicts:
            raise ValueError(
                "params 不能覆盖模型配置字段: " + ", ".join(sorted(conflicts))
            )
        return self


class LanguageModelsConfig(ConfigModel):
    """按配置名组织可用模型，并指定启用的模型。"""

    active: str = Field(min_length=1)  # 启用的模型配置名，须对应 models 中的键。
    models: dict[str, ModelConfig]  # 配置名到模型配置的映射。

    @model_validator(mode="after")
    def validate_active_model(self) -> Self:
        """确保启用的模型配置存在。"""

        if self.active not in self.models:
            raise ValueError("lm_config.active 必须引用 models 中声明的模型")
        return self


class AppConfig(ConfigModel):
    """应用配置根结构，与 YAML 的顶层分组对应。"""

    log: LogConfig  # 日志输出配置。
    server: ServerConfig  # HTTP 服务配置。
    postgresql: PostgresConfig  # AgentScope 持久化连接配置。
    erpnext: ERPNextConfig  # ERPNext 连接与站点配置。
    lm_config: LanguageModelsConfig  # 模型集合及启用配置。


def load_config(config_file: Path = CONFIG_FILE) -> AppConfig:
    """按文件位置解析路径，不依赖工作目录；进程环境变量优先。"""
    load_dotenv(config_file.parent / ".env", override=False)
    loaded = OmegaConf.load(config_file)
    # 展开 ${oc.env:...} 等插值后，再进行类型和必填字段校验。
    resolved = OmegaConf.to_container(loaded, resolve=True)
    return AppConfig.model_validate(resolved)


cfg = load_config()  # 导入时加载并校验配置，配置无效时阻止服务启动。
