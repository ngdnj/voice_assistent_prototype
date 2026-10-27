"""Cloud ASR with Yandex SpeechKit, synchronous recognition API v1.

https://aistudio.yandex.ru/docs/ru/speechkit/stt/api/request-api (checked 2026-10-21):
POST raw audio, up to 30 s, 1 MB, one channel; answer `{"result": "<text>"}`.
Audio is sent as 16 kHz LPCM: 25 s is 800 KB, under the size limit.
Billing is per started 15 s of audio, so a 3 s command costs one unit.
"""

import asyncio
import re
import time

import httpx

from app import metrics
from app.asr.audio import decode_pcm16k, pcm_duration_s
from app.asr.base import MAX_DURATION_S, AsrError, AudioTooLongError, Transcript

RECOGNIZE_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
BACKEND = "yandex"

# API v1 writes times as separate numbers: «в 14 0 0» for 14:00, «в 11 15» for 11:15 (26.10, 11 of 50
# phrases). The agent then has to guess the time, so they are joined back. Minutes are joined only
# after a preposition of time, so «3 литра», «12 яиц» or «25 октября» stay as they are.
_FULL_HOUR_RE = re.compile(r"\b([01]?\d|2[0-3]) 0 0\b")
_HOUR_MINUTES_RE = re.compile(r"\b(в|к|на|до|с|со|по|от|после|около) ([01]?\d|2[0-3]) ([0-5]\d)\b")


def normalize_times(text: str) -> str:
    text = _FULL_HOUR_RE.sub(r"\1:00", text)
    return _HOUR_MINUTES_RE.sub(r"\1 \2:\3", text)


class YandexSpeechKitRecognizer:
    def __init__(
        self,
        api_key: str,
        folder_id: str | None = None,
        timeout_s: float = 10.0,
        max_retries: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._folder_id = folder_id
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Api-Key {api_key}"}, timeout=timeout_s, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _params(self) -> dict[str, str]:
        # rawResults=false: numbers come as digits, like GigaAM.
        params = {"lang": "ru-RU", "format": "lpcm", "sampleRateHertz": "16000", "rawResults": "false"}
        if self._folder_id:
            params["folderId"] = self._folder_id
        return params

    async def _recognize(self, pcm: bytes) -> str:
        """One request plus limited retries on network errors and 429/5xx; 4xx are not retried."""
        for attempt in range(self._max_retries + 1):
            last = attempt == self._max_retries
            try:
                response = await self._client.post(RECOGNIZE_URL, params=self._params(), content=pcm)
            except httpx.TransportError as exc:
                if last:
                    raise AsrError(f"SpeechKit недоступен: {type(exc).__name__}") from exc
            else:
                if response.status_code == 200:
                    return normalize_times(str(response.json().get("result", "")).strip())
                retryable = response.status_code == 429 or response.status_code >= 500
                if last or not retryable:
                    # The body may echo request details; only the status goes into the error.
                    raise AsrError(f"SpeechKit ответил HTTP {response.status_code}")
            await asyncio.sleep(0.5 * (attempt + 1))
        raise AssertionError("unreachable")

    async def transcribe(self, audio: bytes, suffix: str) -> Transcript:
        started = time.perf_counter()
        pcm = await asyncio.to_thread(decode_pcm16k, audio, suffix)
        audio_s = pcm_duration_s(pcm)
        if audio_s > MAX_DURATION_S:
            raise AudioTooLongError(f"Запись {audio_s:.0f} с, максимум {MAX_DURATION_S:.0f} с")
        try:
            text = await self._recognize(pcm)
        except AsrError:
            metrics.ASR_ERRORS.labels(BACKEND).inc()
            raise
        latency = time.perf_counter() - started
        metrics.STAGE_LATENCY.labels("asr").observe(latency)
        metrics.ASR_AUDIO_SECONDS.labels(BACKEND).inc(audio_s)
        return Transcript(text=text, latency_s=latency, audio_s=audio_s)
