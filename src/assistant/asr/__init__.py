from assistant.asr.audio import AudioDecodeError
from assistant.asr.base import MAX_DURATION_S, AsrError, AudioTooLongError, SpeechRecognizer, Transcript
from assistant.config import Settings


def build_recognizer(settings: Settings) -> SpeechRecognizer:
    if settings.asr_backend == "gigaam":
        from assistant.asr.gigaam_asr import GigaAMRecognizer

        return GigaAMRecognizer(settings.gigaam_model)
    if settings.asr_backend == "yandex":
        from assistant.asr.yandex import YandexSpeechKitRecognizer

        if settings.yandex_api_key is None:
            raise ValueError("ASR_BACKEND=yandex требует YANDEX_API_KEY")
        return YandexSpeechKitRecognizer(
            settings.yandex_api_key.get_secret_value(), settings.yandex_folder_id
        )
    raise ValueError(f"Unknown ASR_BACKEND={settings.asr_backend!r}")


__all__ = [
    "MAX_DURATION_S",
    "AsrError",
    "AudioDecodeError",
    "AudioTooLongError",
    "SpeechRecognizer",
    "Transcript",
    "build_recognizer",
]
