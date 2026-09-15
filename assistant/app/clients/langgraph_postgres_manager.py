"""LangGraph PostgreSQL 持久化客户端。"""

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.config.app_config import PostgresConfig


class LangGraphPostgresManager:
    """管理 Checkpoint 连接池和持久化组件的生命周期。"""

    def __init__(self, db_config: PostgresConfig) -> None:
        self._db_config = db_config
        self._pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None
        self._checkpointer: AsyncPostgresSaver | None = None

    async def init(self) -> None:
        """初始化连接池并创建 Checkpoint 所需的数据表。"""
        config = self._db_config
        pool = AsyncConnectionPool[AsyncConnection[DictRow]](
            conninfo=make_conninfo(
                host=config.host,
                port=config.port,
                user=config.user,
                password=config.password.get_secret_value(),
                dbname=config.database,
            ),
            open=False,
            min_size=1,
            max_size=5,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        try:
            await pool.open(wait=True)
            checkpointer = AsyncPostgresSaver(pool)
            await checkpointer.setup()
        except BaseException:
            await pool.close()
            raise
        self._pool = pool
        self._checkpointer = checkpointer

    def get_checkpointer(self) -> AsyncPostgresSaver:
        """获取已初始化的 Checkpoint 持久化组件。"""
        if self._checkpointer is None:
            raise RuntimeError("LangGraph PostgreSQL 管理器尚未初始化")
        return self._checkpointer

    async def close(self) -> None:
        """关闭连接池并清理组件引用。"""
        if self._pool is not None:
            await self._pool.close()
        self._pool = None
        self._checkpointer = None
