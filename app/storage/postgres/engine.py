"""Explicit PostgreSQL engine lifecycle; no connections are created during import."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.storage.postgres.settings import PostgresSettings


async def check_postgres(engine: AsyncEngine) -> bool:
    """Run SELECT 1; connection and timeout errors propagate to the caller."""
    async with engine.connect() as connection:
        return bool(await connection.scalar(text("SELECT 1")) == 1)


@asynccontextmanager
async def postgres_engine(settings: PostgresSettings) -> AsyncIterator[AsyncEngine]:
    """Own one engine/pool for a process lifecycle and dispose it on every exit."""
    engine = create_async_engine(
        settings.url(),
        pool_size=settings.pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        # asyncpg arguments: connection establishment and per-statement timeouts.
        connect_args={"timeout": settings.connect_timeout, "command_timeout": settings.command_timeout},
    )
    try:
        await check_postgres(engine)
        yield engine
    finally:
        await engine.dispose()


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """One AsyncSession per operation; never share a session between concurrent tasks."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def main() -> None:
    """Run a standalone availability check without printing connection details."""
    try:
        async with postgres_engine(PostgresSettings.from_env()):
            print("PostgreSQL доступен")
    except (SQLAlchemyError, OSError, TimeoutError, ValueError):
        raise SystemExit("PostgreSQL недоступен или настройки некорректны") from None


if __name__ == "__main__":
    asyncio.run(main())
