"""按配置名构建模型，使用与 dataagent 相同的服务商和协议选择逻辑。"""

from math import ceil
from typing import cast

from langchain_core.language_models import BaseChatModel, ModelProfile
from langchain_openai import ChatOpenAI
from langchain_openrouter import ChatOpenRouter
from pydantic import SecretStr

from app.agent.responses import DeepSeekResponsesModel, ResponsesModel
from app.config import app_config
from app.errors import AgentError


def create_model(model_name: str) -> BaseChatModel:
    try:
        settings = app_config.cfg.lm_config.models[model_name]
    except KeyError:
        raise AgentError("未找到指定的模型配置。", 503) from None
    if not settings.base_url or not settings.model:
        raise AgentError(
            "请在 conf/app_config.yaml 配置模型，在 conf/.env 填写密钥。", 503
        )
    kwargs = {
        **settings.params,
        "model": settings.model,
        "base_url": settings.base_url,
        "api_key": settings.api_key
        if settings.api_key.get_secret_value()
        else SecretStr("not-configured"),
        "profile": cast(
            ModelProfile,
            {
                **settings.profile.model_dump(exclude_none=True),
                "image_tool_message": settings.api_protocol == "responses"
                and settings.profile.image_inputs,
            },
        ),
        "max_retries": 0,
        "streaming": True,
    }
    if settings.api_protocol == "responses":
        model_class = (
            DeepSeekResponsesModel
            if settings.model_provider == "deepseek"
            else ResponsesModel
        )
        return model_class(
            **kwargs,
            timeout=settings.timeout_seconds,
            use_responses_api=True,
            output_version="responses/v1",
            store=False,
            use_previous_response_id=False,
        )
    if settings.model_provider == "openrouter":
        # ChatOpenRouter 会将 timeout 映射为以毫秒计的 timeout_ms。
        return ChatOpenRouter(**kwargs, timeout=ceil(settings.timeout_seconds * 1000))
    return ChatOpenAI(
        **kwargs, timeout=settings.timeout_seconds, use_responses_api=False
    )
