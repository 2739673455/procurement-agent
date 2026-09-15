"""通过 SQLAlchemy 异步会话读写会话目录。"""

from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors.agent import AgentError
from app.models.conversation import Conversation


class ConversationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        # 每次操作创建独立会话，避免并发任务共享事务状态。
        self.sessions = sessions

    async def list(self, owner: tuple[str, str]):
        statement = (
            select(Conversation.id, Conversation.title, Conversation.updated_at)
            .where(Conversation.site == owner[0], Conversation.username == owner[1])
            .order_by(Conversation.updated_at.desc())
        )
        async with self.sessions() as session:
            rows = await session.execute(statement)
            return [dict(row) for row in rows.mappings()]

    async def create(self, owner: tuple[str, str]) -> str:
        identifier = uuid4()
        async with self.sessions.begin() as session:
            session.add(
                Conversation(
                    id=identifier,
                    site=owner[0],
                    username=owner[1],
                    title="新对话",
                )
            )
        return str(identifier)

    async def require(self, owner: tuple[str, str], identifier: str) -> None:
        statement = select(Conversation.id).where(
            Conversation.id == UUID(identifier),
            Conversation.site == owner[0],
            Conversation.username == owner[1],
        )
        async with self.sessions() as session:
            if await session.scalar(statement) is None:
                raise AgentError("会话不存在或无权访问。", 404)

    async def rename(self, identifier: str, title: str) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.id == UUID(identifier))
                .values(title=title, updated_at=func.now())
            )

    async def touch(self, identifier: str) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.id == UUID(identifier))
                .values(updated_at=func.now())
            )

    async def delete(self, identifier: str) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                delete(Conversation).where(Conversation.id == UUID(identifier))
            )
