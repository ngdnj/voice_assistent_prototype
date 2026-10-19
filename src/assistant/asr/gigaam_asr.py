"""Local ASR with GigaAM (Sber, MIT). Runs on CPU; needs ffmpeg in PATH.

Short-form `transcribe` handles up to 25 s of audio, enough for voice commands.
"""

import asyncio
import os
import tempfile
import threading
import time
from typing import Any

from assistant import metrics
from assistant.asr.base import AudioTooLongError, Transcript

MAX_DURATION_S = 25.0
SAMPLE_RATE = 16000


class GigaAMRecognizer:
    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: Any = None
        # The model is not safe for concurrent inference; one request at a time.
        self._lock = threading.Lock()

    def _load(self) -> Any:
        if self._model is None:
            import gigaam  # heavy import (torch), only when ASR is actually used

            self._model = gigaam.load_model(self._model_name)
        return self._model

    def warm_up(self) -> None:
        with self._lock:
            self._load()

    def _transcribe_file(self, path: str) -> str:
        from gigaam.preprocess import load_audio

        duration_s = load_audio(path).shape[-1] / SAMPLE_RATE
        if duration_s > MAX_DURATION_S:
            raise AudioTooLongError(f"Запись {duration_s:.0f} с, максимум {MAX_DURATION_S:.0f} с")
        with self._lock:
            result = self._load().transcribe(path)
        return str(getattr(result, "text", result)).strip()

    async def transcribe(self, audio: bytes, suffix: str) -> Transcript:
        started = time.perf_counter()
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(audio)
            text = await asyncio.to_thread(self._transcribe_file, path)
        finally:
            os.unlink(path)
        latency = time.perf_counter() - started
        metrics.STAGE_LATENCY.labels("asr").observe(latency)
        return Transcript(text=text, latency_s=latency)
