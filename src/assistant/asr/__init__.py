from assistant.asr.base import AudioTooLongError, SpeechRecognizer, Transcript
from assistant.config import Settings


def build_recognizer(settings: Settings) -> SpeechRecognizer:
    if settings.asr_backend == "gigaam":
        from assistant.asr.gigaam_asr import GigaAMRecognizer

        return GigaAMRecognizer(settings.gigaam_model)
    raise ValueError(f"Unknown ASR_BACKEND={settings.asr_backend!r}")


__all__ = ["AudioTooLongError", "SpeechRecognizer", "Transcript", "build_recognizer"]
