"""Builds the ASR adapter selected by ASR_BACKEND."""

from app.asr.base import SpeechRecognizer
from app.config import Settings


def _yandex(settings: Settings, variable: str) -> SpeechRecognizer:
    from app.asr.yandex import YandexSpeechKitRecognizer

    if settings.yandex_api_key is None:
        raise ValueError(f"{variable}=yandex требует YANDEX_API_KEY")
    return YandexSpeechKitRecognizer(settings.yandex_api_key.get_secret_value(), settings.yandex_folder_id)


def build_recognizer(settings: Settings) -> SpeechRecognizer:
    if settings.asr_backend == "gigaam":
        from app.asr.gigaam_asr import GigaAMRecognizer

        local = GigaAMRecognizer(settings.gigaam_model)
        if settings.asr_overflow == "none":
            return local
        from app.asr.overflow import OverflowRecognizer

        backup = _yandex(settings, "ASR_OVERFLOW")
        return OverflowRecognizer(local, backup, max_in_primary=settings.asr_overflow_queue)
    if settings.asr_backend == "yandex":
        return _yandex(settings, "ASR_BACKEND")
    raise ValueError(f"Unknown ASR_BACKEND={settings.asr_backend!r}")
