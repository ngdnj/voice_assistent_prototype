"""Local ASR first, a cloud backup when the local queue is long or the local model fails.

GigaAM recognizes one message at a time. Measured on 26.10 (evals/asr_load.py, 2 torch threads as an
approximation of a 2 vCPU VM): every message waiting in the queue adds about 0.8 s, 20 messages at
once wait up to 15 s. Above `max_in_primary` messages in the local queue the next one goes to the
cloud, which scales by itself and costs money only at peaks.

The counter is per process: with several ASR worker processes each one keeps its own limit.
"""

from app import metrics
from app.asr.base import AsrError, SpeechRecognizer, Transcript


class OverflowRecognizer:
    def __init__(self, primary: SpeechRecognizer, backup: SpeechRecognizer, *, max_in_primary: int) -> None:
        if max_in_primary < 1:
            raise ValueError("max_in_primary must be at least 1")
        self._primary = primary
        self._backup = backup
        self._max_in_primary = max_in_primary
        # Messages being recognized or waiting for the local model right now.
        self._in_primary = 0

    async def transcribe(self, audio: bytes, suffix: str) -> Transcript:
        if self._in_primary >= self._max_in_primary:
            metrics.ASR_OVERFLOW.labels("busy").inc()
            return await self._backup.transcribe(audio, suffix)
        self._in_primary += 1
        try:
            return await self._primary.transcribe(audio, suffix)
        except AsrError:
            # A provider failure; too long or unreadable audio is the user's input and is not retried.
            metrics.ASR_OVERFLOW.labels("primary_error").inc()
        finally:
            self._in_primary -= 1
        return await self._backup.transcribe(audio, suffix)

    async def aclose(self) -> None:
        try:
            await self._primary.aclose()
        finally:
            await self._backup.aclose()
