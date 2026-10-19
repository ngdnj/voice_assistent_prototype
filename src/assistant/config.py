"""Application settings, loaded from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM: "cloudru" — Cloud.ru Foundation Models (OpenAI-compatible), "gigachat" — Sber GigaChat API
    llm_provider: Literal["cloudru", "gigachat"] = "cloudru"
    llm_temperature: float = 0.1
    llm_timeout_s: float = 60.0
    # Prices in rubles per 1000 tokens, used for cost analytics (p50 / p95).
    llm_price_input_rub_per_1k: float = 0.0
    llm_price_output_rub_per_1k: float = 0.0

    cloudru_api_key: SecretStr | None = None
    cloudru_base_url: str = "https://foundation-models.api.cloud.ru/v1"
    cloudru_model: str = "ai-sage/GigaChat3.5-432B-A28B-Reasoning"

    gigachat_credentials: SecretStr | None = None
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2-Max"
    gigachat_verify_ssl: bool = True
    gigachat_ca_bundle_file: str | None = None

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

    @property
    def llm_model(self) -> str:
        return self.cloudru_model if self.llm_provider == "cloudru" else self.gigachat_model

    @property
    def pricing(self) -> tuple[float, float]:
        return self.llm_price_input_rub_per_1k, self.llm_price_output_rub_per_1k

    def with_llm_model(self, model: str) -> "Settings":
        field = "cloudru_model" if self.llm_provider == "cloudru" else "gigachat_model"
        return self.model_copy(update={field: model})


@lru_cache
def get_settings() -> Settings:
    return Settings()
