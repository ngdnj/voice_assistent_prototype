"""Cloud.ru Foundation Models as an alternative LLM for eval comparison only.

The product stack is GigaChat API (AGENTS §3); Cloud.ru stays here until the team
decides otherwise. The API is OpenAI-compatible, so ChatOpenAI is used.

    uv run python -m evals.run --provider cloudru --model ai-sage/GigaChat3.5-432B-A28B-Reasoning
"""

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import Settings
from app.llm.model import MissingCredentialsError


class CloudRuSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    cloudru_api_key: SecretStr | None = None
    cloudru_base_url: str = "https://foundation-models.api.cloud.ru/v1"
    cloudru_model: str = "ai-sage/GigaChat3.5-432B-A28B-Reasoning"


def build_cloudru_model(settings: Settings, cloudru: CloudRuSettings) -> BaseChatModel:
    """Timeouts and temperature come from the shared settings, so runs are comparable."""
    from langchain_openai import ChatOpenAI

    if cloudru.cloudru_api_key is None:
        raise MissingCredentialsError("CLOUDRU_API_KEY is not set, see .env.example")
    return ChatOpenAI(
        model=cloudru.cloudru_model,
        api_key=cloudru.cloudru_api_key,
        base_url=cloudru.cloudru_base_url,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
    )
