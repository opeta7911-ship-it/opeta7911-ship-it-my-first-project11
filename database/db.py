from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from database.models import Base

_MIGRATIONS = [
    "ALTER TABLE filters ADD COLUMN limit_reset_at DATETIME",
    "ALTER TABLE lots ADD COLUMN expires_at DATETIME",
]


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url, echo=False)
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for sql in _MIGRATIONS:
                try:
                    await conn.execute(text(sql))
                except Exception:
                    pass  # column already exists

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()
