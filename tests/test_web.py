from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from assistant.asr.base import AudioTooLongError, Transcript
from assistant.demo import web
from assistant.llm.agent import AgentReply


class FakeAssistant:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.timezones: list[str] = []

    async def handle(self, user_id: str, text: str, *, reference_time: datetime, timezone: str) -> AgentReply:
        assert reference_time.tzinfo is not None
        self.calls.append((user_id, text))
        self.timezones.append(timezone)
        return AgentReply(text=f"ok: {text}")

    async def reset(self, user_id: str) -> None:
        pass


class FakeRecognizer:
    def __init__(self, text: str = "", too_long: bool = False) -> None:
        self.text = text
        self.too_long = too_long

    async def transcribe(self, audio: bytes, suffix: str) -> Transcript:
        if self.too_long:
            raise AudioTooLongError("Запись 40 с, максимум 25 с")
        return Transcript(self.text, 0.1)

    async def aclose(self) -> None:
        pass


@pytest.fixture
def client() -> TestClient:
    # No `with`: skip the lifespan, which needs real credentials.
    return TestClient(web.app)


@pytest.fixture
def assistant() -> FakeAssistant:
    fake = FakeAssistant()
    web.state.assistant = fake  # type: ignore[assignment]
    return fake


def test_chat_assigns_stable_user_cookie(client: TestClient, assistant: FakeAssistant) -> None:
    first = client.post("/api/chat", json={"text": "Что у меня сегодня?"})
    assert first.status_code == 200
    assert first.json()["text"] == "ok: Что у меня сегодня?"
    client.post("/api/chat", json={"text": "А завтра?"})
    assert assistant.calls[0][0] == assistant.calls[1][0]


def test_chat_rejects_empty_text(client: TestClient, assistant: FakeAssistant) -> None:
    assert client.post("/api/chat", json={"text": ""}).status_code == 422


def test_voice_passes_transcript_to_agent(client: TestClient, assistant: FakeAssistant) -> None:
    web.state.recognizer = FakeRecognizer("Добавь встречу с командой завтра в 15:00")
    res = client.post("/api/voice", files={"audio": ("v.webm", b"\x00" * 100, "audio/webm")})
    assert res.status_code == 200
    assert res.json()["transcript"] == "Добавь встречу с командой завтра в 15:00"
    assert assistant.calls[0][1] == "Добавь встречу с командой завтра в 15:00"


def test_voice_with_silence_does_not_call_agent(client: TestClient, assistant: FakeAssistant) -> None:
    web.state.recognizer = FakeRecognizer("")
    res = client.post("/api/voice", files={"audio": ("v.webm", b"\x00", "audio/webm")})
    assert res.status_code == 200
    assert assistant.calls == []


def test_voice_rejects_unknown_format_and_long_audio(client: TestClient, assistant: FakeAssistant) -> None:
    web.state.recognizer = FakeRecognizer(too_long=True)
    exe = {"audio": ("a.exe", b"x", "application/x-msdownload")}
    assert client.post("/api/voice", files=exe).status_code == 415
    long_audio = {"audio": ("v.webm", b"x", "audio/webm")}
    assert client.post("/api/voice", files=long_audio).status_code == 422


def test_metrics_endpoint(client: TestClient) -> None:
    res = client.get("/metrics/")
    assert res.status_code == 200
    assert "assistant_request_cost_rub" in res.text


def test_transcribe_returns_text_without_calling_agent(client: TestClient, assistant: FakeAssistant) -> None:
    web.state.recognizer = FakeRecognizer("Что у меня сегодня?")
    res = client.post("/api/transcribe", files={"audio": ("v.webm", b"\x00" * 100, "audio/webm")})
    assert res.status_code == 200
    assert res.json() == {"transcript": "Что у меня сегодня?", "asr_latency_s": 0.1}
    assert assistant.calls == []
