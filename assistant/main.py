"""应用组件装配与本地服务启动入口。"""

import subprocess
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.conversations import router
from app.config import app_config
from app.database.postgres import open_postgres
from app.errors import AgentError
from app.models.conversation import Conversation
from app.repositories.conversations import ConversationRepository
from app.services.conversations import AgentManager


@asynccontextmanager
async def lifespan(app):
    async with open_postgres(app_config.cfg.langgraph_postgresql) as (engine, saver):
        async with engine.begin() as connection:
            await connection.run_sync(Conversation.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        repository = ConversationRepository(sessions)
        manager = AgentManager(saver, repository)
        app.state.manager = manager
        try:
            yield
        finally:
            await manager.close()


app = FastAPI(title="Procurement Assistant", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(AgentError)
async def agent_error(_request, exc):
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.exception_handler(Exception)
async def unexpected_error(_request, _exc):
    return JSONResponse({"error": "助手服务发生错误，请稍后重试。"}, status_code=500)


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
