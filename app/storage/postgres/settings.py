"""PostgreSQL connection settings read explicitly from the environment."""

import math
import os
from dataclasses import dataclass, field

from sqlalchemy import URL


@dataclass(frozen=True)
class PostgresSettings:
    host: str = "127.0.0.1"
    port: int = 5433
    database: str = "it_gorky"
    user: str = "it_gorky"
    password: str | None = field(default=None, repr=False)
    connect_timeout: float = 5.0
    command_timeout: float = 5.0
    pool_size: int = 5

    def __post_init__(self) -> None:
        for name, value in (
            ("POSTGRES_HOST", self.host),
            ("POSTGRES_DB", self.database),
            ("POSTGRES_USER", self.user),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        if not 1 <= self.port <= 65535:
            raise ValueError("POSTGRES_PORT must be between 1 and 65535")
        for name, timeout in (
            ("POSTGRES_CONNECT_TIMEOUT", self.connect_timeout),
            ("POSTGRES_COMMAND_TIMEOUT", self.command_timeout),
        ):
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 1 <= self.pool_size <= 50:
            raise ValueError("POSTGRES_POOL_SIZE must be between 1 and 50")

    @classmethod
    def from_env(cls) -> "PostgresSettings":
        return cls(
            host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
            port=int(os.environ.get("POSTGRES_PORT", "5433")),
            database=os.environ.get("POSTGRES_DB", "it_gorky"),
            user=os.environ.get("POSTGRES_USER", "it_gorky"),
            password=os.environ.get("POSTGRES_PASSWORD") or None,
            connect_timeout=float(os.environ.get("POSTGRES_CONNECT_TIMEOUT", "5")),
            command_timeout=float(os.environ.get("POSTGRES_COMMAND_TIMEOUT", "5")),
            pool_size=int(os.environ.get("POSTGRES_POOL_SIZE", "5")),
        )

    def url(self) -> URL:
        """SQLAlchemy URL; its string form masks the password, but never log it anyway."""
        return URL.create(
            "postgresql+asyncpg",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
        )
