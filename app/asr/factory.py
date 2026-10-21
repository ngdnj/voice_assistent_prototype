"""Builds the ASR adapter selected by ASR_BACKEND."""

from app.asr.base import SpeechRecognizer
from app.config import Settings


def build_recognizer(settings: Settings) -> SpeechRecognizer:
    if settings.asr_backend == "gigaam":
        from app.asr.gigaam_asr import GigaAMRecognizer

        return GigaAMRecognizer(settings.gigaam_model)
    if settings.asr_backend == "yandex":
        from app.asr.yandex import YandexSpeechKitRecognizer

        if settings.yandex_api_key is None:
            raise ValueError("ASR_BACKEND=yandex требует YANDEX_API_KEY")
        return YandexSpeechKitRecognizer(
            settings.yandex_api_key.get_secret_value(), settings.yandex_folder_id
        )
    raise ValueError(f"Unknown ASR_BACKEND={settings.asr_backend!r}")
