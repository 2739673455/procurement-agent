"""参考 dataagent 的 dotenv/OmegaConf 加载流程，提供带类型校验的 YAML 配置。"""

from pathlib import Path
from typing import Any, Literal, Self

from dotenv import load_dotenv
from omegaconf import OmegaConf
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT_DIR / "conf"
CONFIG_FILE = CONFIG_DIR / "app_config.yaml"


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LogConfig(ConfigModel):
    """日志级别和滚动配置。"""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    rotation: str = "10 MB"


class ServerConfig(ConfigModel):
    host: str
    port: int = Field(ge=1, le=65535)


class PostgresConfig(ConfigModel):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)
    database: str = Field(min_length=1)


class ERPNextConfig(ConfigModel):
    base_url: str = Field(min_length=1)
    site: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)


class ModelProfileConfig(ConfigModel):
    """声明模型能力，未知的上下文大小不设置。"""

    image_inputs: bool
    structured_output: bool
    max_input_tokens: int | None = Field(default=None, gt=0)


class ModelConfig(ConfigModel):
    model_provider: str = Field(min_length=1)
    api_protocol: Literal["chat_completions", "responses"]
    profile: ModelProfileConfig
    # 模型名称和接口地址可留空，便于在配置模型服务前先启动应用。
    model: str
    base_url: str
    api_key: SecretStr
    timeout_seconds: float = Field(gt=0)
    params: dict[str, Any]

    @model_validator(mode="after")
    def validate_params(self) -> Self:
        reserved = {
            "model",
            "model_name",
            "model_provider",
            "api_protocol",
            "profile",
            "base_url",
            "openai_api_base",
            "api_key",
            "openai_api_key",
            "timeout",
            "request_timeout",
            "streaming",
            "use_responses_api",
            "use_previous_response_id",
            "output_version",
            "store",
        }
        conflicts = reserved & self.params.keys()
        if conflicts:
            raise ValueError(
                "params 不能覆盖模型配置字段: " + ", ".join(sorted(conflicts))
            )
        return self


class LanguageModelsConfig(ConfigModel):
    active: str = Field(min_length=1)
    models: dict[str, ModelConfig]

    @model_validator(mode="after")
    def validate_active_model(self) -> Self:
        if self.active not in self.models:
            raise ValueError("lm_config.active 必须引用 models 中声明的模型")
        return self


class AppConfig(ConfigModel):
    log: LogConfig
    server: ServerConfig
    langgraph_postgresql: PostgresConfig
    erpnext: ERPNextConfig
    lm_config: LanguageModelsConfig


def load_config(config_file: Path = CONFIG_FILE) -> AppConfig:
    """按文件位置解析路径，不依赖工作目录；进程环境变量优先。"""
    load_dotenv(config_file.parent / ".env", override=False)
    loaded = OmegaConf.load(config_file)
    resolved = OmegaConf.to_container(loaded, resolve=True)
    return AppConfig.model_validate(resolved)


cfg = load_config()
