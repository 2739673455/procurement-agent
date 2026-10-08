"""应用组件装配与本地服务启动入口。"""

import subprocess
from contextlib import asynccontextmanager

import uvicorn
from agentscope.app.storage import AsyncSQLAlchemyStorage
from fastapi import FastAPI
from loguru import logger
from sqlalchemy import URL

from app.agent.runtime import create_runtime
from app.api.conversations import router
from app.config import app_config
from app.errors.base import ProblemDetails
from app.errors.exc_handlers import register_exception_handlers
from app.observability.log import setup_logger
from app.observability.trace import TraceMiddleware
from app.services.conversations import ConversationService


@asynccontextmanager
async def lifespan(app):
    logger.info("开始初始化应用资源")
    settings = app_config.cfg.postgresql
    url = URL.create(
        "postgresql+psycopg",
        username=settings.user,
        password=settings.password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        database=settings.database,
    )
    runtime = create_runtime(
        AsyncSQLAlchemyStorage(
            url.render_as_string(hide_password=False),
            engine_kwargs={"pool_pre_ping": True, "hide_parameters": True},
        )
    )
    async with runtime.router.lifespan_context(runtime):
        app.state.conversations = ConversationService(runtime.state)
        logger.info("应用资源初始化完成")
        yield
    logger.info("应用资源释放完成")
    await logger.complete()


setup_logger()
app = FastAPI(
    title="Procurement Assistant",
    lifespan=lifespan,
    responses={
        "default": {
            "model": ProblemDetails,
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": "#/components/schemas/ProblemDetails"}
                }
            },
        }
    },
)
app.include_router(router)
app.add_middleware(TraceMiddleware)
register_exception_handlers(app)


def listen_host(host: str) -> str:
    if host:
        return host
    result = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{range .IPAM.Config}}{{.Gateway}}{{end}}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if not result.stdout.strip():
        raise RuntimeError("请在 conf/app_config.yaml 设置 server.host。")
    return result.stdout.strip()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=listen_host(app_config.cfg.server.host),
        port=app_config.cfg.server.port,
        access_log=False,
    )
