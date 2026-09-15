"""应用组件装配与本地服务启动入口。"""

import subprocess
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from fastapi import FastAPI
from loguru import logger

from app.api.conversations import router
from app.clients.langgraph_postgres_manager import LangGraphPostgresManager
from app.clients.postgres_client_manager import PostgresClientManager
from app.config import app_config
from app.errors.base import ProblemDetails
from app.errors.exc_handlers import register_exception_handlers
from app.models.conversation import Conversation
from app.observability.log import setup_logger
from app.observability.trace import TraceMiddleware
from app.repositories.conversations import ConversationRepository
from app.services.conversations import ConversationService
from app.services.runs import AgentRunService


@asynccontextmanager
async def lifespan(app):
    logger.info("开始初始化应用资源")
    async with AsyncExitStack() as stack:
        postgres = PostgresClientManager(
            app_config.cfg.langgraph_postgresql, Conversation
        )
        stack.push_async_callback(postgres.close)
        postgres.init()
        await postgres.init_tables()

        persistence = LangGraphPostgresManager(app_config.cfg.langgraph_postgresql)
        stack.push_async_callback(persistence.close)
        await persistence.init()

        repository = ConversationRepository(postgres.session_maker)
        runs = AgentRunService(persistence.get_checkpointer(), repository)
        stack.push_async_callback(runs.close)
        app.state.conversations = ConversationService(repository, runs)
        logger.info("应用资源初始化完成")
        try:
            yield
        finally:
            logger.info("开始释放应用资源")
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
