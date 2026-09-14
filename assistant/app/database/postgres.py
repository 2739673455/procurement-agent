"""SQLAlchemy 引擎、Checkpoint 连接池与数据库进程锁。"""

from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import create_async_engine

from app.config.app_config import PostgresConfig


@asynccontextmanager
async def open_postgres(config: PostgresConfig):
    conninfo = make_conninfo(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password.get_secret_value(),
        dbname=config.database,
    )
    async with AsyncConnectionPool[AsyncConnection[DictRow]](
        conninfo,
        open=False,
        min_size=1,
        max_size=5,  # 其中一个连接用于持有进程锁。
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    ) as pool:
        await pool.wait()
        # 运行订阅保存在当前进程中，因此拒绝第二个服务进程使用同一数据库。
        async with pool.connection() as lease:
            row = await (
                await lease.execute("SELECT pg_try_advisory_lock(810013) AS acquired")
            ).fetchone()
            if not row or not row["acquired"]:
                raise RuntimeError("同一数据库只支持一个 Agent 服务进程。")
            try:
                saver = AsyncPostgresSaver(pool)
                await saver.setup()
                # ORM 和官方 Checkpoint 分别管理连接，避免混用事务模式。
                engine = create_async_engine(
                    URL.create(
                        "postgresql+psycopg",
                        username=config.user,
                        password=config.password.get_secret_value(),
                        host=config.host,
                        port=config.port,
                        database=config.database,
                    ),
                    pool_size=5,
                    max_overflow=0,
                    pool_pre_ping=True,
                    hide_parameters=True,
                )
                try:
                    yield engine, saver
                finally:
                    await engine.dispose()
            finally:
                await lease.execute("SELECT pg_advisory_unlock(810013)")
