"""Settings of the ML adapters (LLM, ASR) and the agent, loaded from environment variables and `.env`."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM: GigaChat API (AGENTS §3)
    gigachat_credentials: SecretStr | None = None
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2-Max"
    gigachat_verify_ssl: bool = True
    gigachat_ca_bundle_file: str | None = None

    llm_temperature: float = 0.1
    # Per LLM call. A call that hangs is cut off and retried: a fresh attempt is
    # usually fast, while waiting out a slow one costs the user 20+ seconds.
    llm_timeout_s: float = 12.0
    llm_max_retries: int = 1
    # Prices in rubles per 1000 tokens, used for cost analytics (p50 / p95).
    llm_price_input_rub_per_1k: float = 0.0
    llm_price_output_rub_per_1k: float = 0.0

    # delete_event is not one of the three MVP MCP tools (AGENTS §8): until role 4 agrees
    # on its contract, the prompt does not mention it and the agent does not get the tool.
    enable_delete_event: bool = False

    # ASR
    asr_backend: Literal["gigaam", "yandex"] = "gigaam"
    gigaam_model: str = "v3_e2e_rnnt"
    # Yandex SpeechKit: API key of a service account with the ai.speechkit-stt.user role.
    # folder_id is needed only for a user account (IAM token), not for a service account key.
    yandex_api_key: SecretStr | None = None
    yandex_folder_id: str | None = None

    # Dialog context
    redis_url: str | None = "redis://localhost:6379/0"
    # Stored turns per dialog; what goes into the prompt is further cut by the token budget.
    history_max_turns: int = 20
    history_max_tokens: int = 3000
    # AGENTS §10: seven days, refreshed on every saved turn.
    history_ttl_s: int = 7 * 24 * 3600

    # MCP server of role 4, e.g. http://localhost:8001/mcp
    mcp_url: str | None = None

    @property
    def llm_model(self) -> str:
        return self.gigachat_model

    @property
    def pricing(self) -> tuple[float, float]:
        return self.llm_price_input_rub_per_1k, self.llm_price_output_rub_per_1k

    def with_llm_model(self, model: str) -> "Settings":
        return self.model_copy(update={"gigachat_model": model})


@lru_cache
def get_settings() -> Settings:
    return Settings()
