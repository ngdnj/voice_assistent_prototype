"""Application settings, loaded from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM (GigaChat)
    gigachat_credentials: SecretStr | None = None
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2-Max"
    gigachat_verify_ssl: bool = True
    gigachat_ca_bundle_file: str | None = None
    llm_temperature: float = 0.1
    llm_timeout_s: float = 60.0
    # Price in rubles per 1000 tokens, used for cost analytics (p50 / p95).
    llm_price_rub_per_1k_tokens: float = 1.5

    # ASR
    asr_backend: str = "gigaam"
    gigaam_model: str = "v3_e2e_rnnt"

    # Dialog context
    redis_url: str | None = "redis://localhost:6379/0"
    history_max_turns: int = 10
    history_ttl_s: int = 24 * 3600

    # MCP server
    mcp_url: str | None = None  # e.g. http://localhost:8001/mcp; None = in-process server
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8001
    storage_path: Path = Path("data/events.sqlite3")

    timezone: str = "Europe/Moscow"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
