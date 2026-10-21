"""Speech-to-text interface shared by all ASR backends."""

from dataclasses import dataclass
from typing import Protocol

# Voice commands are short; longer input is rejected before it reaches a model or a paid API.
MAX_DURATION_S = 25.0


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str  # empty when no speech was recognized
    latency_s: float  # decoding + recognition, wall clock
    audio_s: float = 0.0  # audio duration: the unit of ASR cost


class AudioTooLongError(ValueError):
    pass


class AsrError(RuntimeError):
    """The provider failed (network, HTTP error). The message is safe to log: no keys, no audio."""


class SpeechRecognizer(Protocol):
    async def transcribe(self, audio: bytes, suffix: str) -> Transcript:
        """Recognize Russian speech. `suffix` is the container extension, e.g. ".webm".

        Raises AudioTooLongError (> MAX_DURATION_S), AudioDecodeError (unreadable audio)
        and AsrError (provider failure). Never blocks the event loop.
        """
        ...

    async def aclose(self) -> None:
        """Release clients and threads; called once on shutdown."""
        ...
