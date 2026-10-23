"""Alembic environment: connection from PostgresSettings, schema from app.storage.postgres.tables."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.storage.postgres.settings import PostgresSettings
from app.storage.postgres.tables import metadata

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = metadata


def _settings() -> PostgresSettings:
    # Tests pass settings of the dedicated test database; the CLI reads POSTGRES_* from the environment.
    settings = config.attributes.get("postgres_settings")
    return settings if isinstance(settings, PostgresSettings) else PostgresSettings.from_env()


def run_migrations_offline() -> None:
    """Emit SQL for review (`alembic upgrade head --sql`) without connecting."""
    context.configure(
        url=_settings().url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    settings = _settings()
    engine = create_async_engine(
        settings.url(),
        poolclass=pool.NullPool,
        connect_args={"timeout": settings.connect_timeout},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
