"""Chat model factory."""

from langchain_core.language_models import BaseChatModel

from assistant.config import Settings


class MissingCredentialsError(RuntimeError):
    pass


def build_chat_model(settings: Settings) -> BaseChatModel:
    if settings.llm_provider == "cloudru":
        return _cloudru(settings)
    return _gigachat(settings)


def _cloudru(settings: Settings) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    if settings.cloudru_api_key is None:
        raise MissingCredentialsError("CLOUDRU_API_KEY is not set, see .env.example")
    return ChatOpenAI(
        model=settings.cloudru_model,
        api_key=settings.cloudru_api_key,
        base_url=settings.cloudru_base_url,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_s,
        max_retries=2,
    )


def _gigachat(settings: Settings) -> BaseChatModel:
    from langchain_gigachat import GigaChat

    if settings.gigachat_credentials is None:
        raise MissingCredentialsError("GIGACHAT_CREDENTIALS is not set, see .env.example")
    return GigaChat(
        credentials=settings.gigachat_credentials.get_secret_value(),
        scope=settings.gigachat_scope,
        model=settings.gigachat_model,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_s,
        verify_ssl_certs=settings.gigachat_verify_ssl,
        ca_bundle_file=settings.gigachat_ca_bundle_file,
    )
