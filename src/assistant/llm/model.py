"""Chat model factory."""

from langchain_core.language_models import BaseChatModel
from langchain_gigachat import GigaChat

from assistant.config import Settings


class MissingCredentialsError(RuntimeError):
    pass


def build_chat_model(settings: Settings) -> BaseChatModel:
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
