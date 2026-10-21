"""Chat model factory."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from gigachat.context import session_id_cvar
from langchain_core.language_models import BaseChatModel

from assistant.config import Settings


@contextmanager
def llm_session(user_id: str) -> Iterator[None]:
    """Bind LLM calls to a per-user session.

    GigaChat caches the request prefix within an X-Session-ID, which cuts both
    latency and billed input tokens on follow-up calls. A hash is sent instead
    of the raw user id. Other providers ignore it.
    """
    token = session_id_cvar.set(hashlib.sha256(user_id.encode()).hexdigest()[:32])
    try:
        yield
    finally:
        session_id_cvar.reset(token)


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
        max_retries=settings.llm_max_retries,
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
        max_retries=settings.llm_max_retries,
        verify_ssl_certs=settings.gigachat_verify_ssl,
        ca_bundle_file=settings.gigachat_ca_bundle_file,
    )
