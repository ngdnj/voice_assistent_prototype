"""Yandex SpeechKit adapter against a mocked HTTP transport: no network, no ffmpeg."""

import httpx
import pytest

from assistant.asr import AsrError, AudioTooLongError
from assistant.asr import yandex as yandex_module
from assistant.asr.audio import BYTES_PER_SECOND
from assistant.asr.yandex import YandexSpeechKitRecognizer

PCM_3S = b"\x00" * (3 * BYTES_PER_SECOND)


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture(autouse=True)
def fake_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yandex_module, "decode_pcm16k", lambda audio, suffix: audio)
    monkeypatch.setattr(yandex_module.asyncio, "sleep", _no_sleep)


def recognizer(transport: httpx.MockTransport, folder_id: str | None = None) -> YandexSpeechKitRecognizer:
    return YandexSpeechKitRecognizer("test-key", folder_id, transport=transport)


async def test_sends_lpcm_with_api_key_and_returns_text() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": "встреча завтра в 10:00"})

    asr = recognizer(httpx.MockTransport(handle), folder_id="b1g")
    transcript = await asr.transcribe(PCM_3S, ".webm")
    await asr.aclose()

    assert transcript.text == "встреча завтра в 10:00"
    assert transcript.audio_s == 3.0
    [request] = seen
    assert request.headers["Authorization"] == "Api-Key test-key"
    assert request.url.params["format"] == "lpcm"
    assert request.url.params["sampleRateHertz"] == "16000"
    assert request.url.params["folderId"] == "b1g"
    assert request.content == PCM_3S


async def test_retries_server_error_once() -> None:
    statuses = iter([503, 200])

    def handle(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, json={"result": "ок"} if status == 200 else {})

    asr = recognizer(httpx.MockTransport(handle))
    assert (await asr.transcribe(PCM_3S, ".ogg")).text == "ок"


async def test_client_error_is_not_retried_and_hides_body() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error_message": "key test-key is invalid"})

    asr = recognizer(httpx.MockTransport(handle))
    with pytest.raises(AsrError) as error:
        await asr.transcribe(PCM_3S, ".ogg")
    assert calls == 1
    assert "401" in str(error.value)
    assert "test-key" not in str(error.value)


async def test_network_error_becomes_asr_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(AsrError):
        await recognizer(httpx.MockTransport(handle)).transcribe(PCM_3S, ".ogg")


async def test_long_audio_is_rejected_before_request() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    with pytest.raises(AudioTooLongError):
        await recognizer(httpx.MockTransport(handle)).transcribe(b"\x00" * (26 * BYTES_PER_SECOND), ".ogg")
