"""Speech-to-text interface shared by all ASR backends."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    latency_s: float


class AudioTooLongError(ValueError):
    pass


class SpeechRecognizer(Protocol):
    async def transcribe(self, audio: bytes, suffix: str) -> Transcript:
        """Recognize Russian speech. `suffix` is the container extension, e.g. ".webm"."""
        ...
