"""应用组件装配与本地服务启动入口。"""

import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from agentscope.agent import InjectionConfig
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import DockerWorkspaceManager, IsolationPolicy
from fastapi import FastAPI
from loguru import logger
from sqlalchemy import URL

from app.api.sessions import router
from app.config import agents, mcp
from app.config.app import cfg
from app.config.loader import ROOT_DIR
from app.errors.base import ProblemDetails
from app.errors.exc_handlers import register_exception_handlers
from app.observability.log import setup_logger
from app.observability.trace import TraceMiddleware
from app.runtime import AgentCatalog, create_runtime
from app.services.sessions import SessionService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """初始化框架运行服务和业务 HTTP 连接池，在后台任务退出后释放连接。"""
    logger.info("开始初始化应用资源")
    settings = cfg.postgresql
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
        ),
        catalog=AgentCatalog(
            definitions=agents.cfg,
            mcps=mcp.cfg.servers,
            base_dir=ROOT_DIR,
            skills_dir=ROOT_DIR / "resources" / "skills",
        ),
        message_bus=InMemoryMessageBus(),
        workspace_manager=DockerWorkspaceManager(
            str(ROOT_DIR / "data" / "workspaces"),
            isolation=IsolationPolicy.PER_USER,
            base_image=cfg.workspace.base_image,
            node_version=cfg.workspace.node_version,
            extra_pip=cfg.workspace.extra_pip,
            ttl=cfg.workspace.ttl_seconds,
            sweep_interval=cfg.workspace.sweep_interval_seconds,
        ),
        injection_config=InjectionConfig(timezone=cfg.runtime.timezone),
        context_ttl_seconds=cfg.runtime.context_ttl_seconds,
    )
    erpnext = cfg.erpnext
    async with (
        httpx.AsyncClient(
            base_url=erpnext.base_url,
            headers={"Host": erpnext.site},
            timeout=erpnext.timeout_seconds,
        ) as erpnext_http,
        runtime,
    ):
        app.state.erpnext_http = erpnext_http
        app.state.sessions = SessionService(runtime)
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
    """使用显式监听地址；为空时读取 Docker 默认网桥网关地址。"""
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
        raise RuntimeError("请在 conf/app.yaml 设置 server.host。")
    return result.stdout.strip()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=listen_host(cfg.server.host),
        port=cfg.server.port,
        access_log=False,
    )
