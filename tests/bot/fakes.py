"""In-memory doubles of the bot ports; no MAX, Redis, ASR or LLM."""

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime

from app.asr.base import Transcript
from app.bot.client import Button, SendResult
from app.bot.service import BackendRequest, BotService

NOW = datetime(2026, 10, 25, 9, 30, tzinfo=UTC)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.edited: list[tuple[str, str]] = []
        self.actions: list[str] = []
        self.edit_result = SendResult("delivered", external_message_id="mid.1")
        self.result = SendResult("delivered", external_message_id="mid.1")
        self.buttons: list[tuple[Button, ...]] = []
        self.deleted: list[str] = []
        self.answered: list[str] = []

    async def send_text(self, *, chat_id: str, text: str, buttons: tuple[Button, ...] = ()) -> SendResult:
        self.sent.append((chat_id, text))
        self.actions.append(text)
        self.buttons.append(buttons)
        return self.result

    async def edit_text(self, *, message_id: str, text: str) -> SendResult:
        self.edited.append((message_id, text))
        self.actions.append(text)
        return self.edit_result

    async def delete_message(self, *, message_id: str) -> str:
        self.deleted.append(message_id)
        return ""

    async def answer_callback(self, *, callback_id: str) -> str:
        self.answered.append(callback_id)
        return ""

    @property
    def texts(self) -> list[str]:
        # Attempted text updates, in order; sent/edited distinguish transport calls.
        return self.actions.copy()


class FakeGate:
    def __init__(self) -> None:
        self.events: set[str] = set()
        self.locks: dict[str, str] = {}
        self.fail = False

    async def first_seen(self, event_id: str) -> bool:
        if self.fail:
            raise ConnectionError("redis down")
        if event_id in self.events:
            return False
        self.events.add(event_id)
        return True

    async def forget(self, event_id: str) -> None:
        self.events.discard(event_id)

    async def is_busy(self, owner_id: str) -> bool:
        return owner_id in self.locks

    async def acquire(self, owner_id: str, job_id: str) -> bool:
        if owner_id in self.locks:
            return False
        self.locks[owner_id] = job_id
        return True

    async def release(self, owner_id: str, job_id: str) -> bool:
        if self.locks.get(owner_id) != job_id:
            return False
        del self.locks[owner_id]
        return True


class FakeBackend:
    def __init__(self, reply: str = "Записал задачу «Отчёт»") -> None:
        self.reply = reply
        self.requests: list[BackendRequest] = []
        self.release = asyncio.Event()
        self.release.set()
        self.error: Exception | None = None

    async def answer(self, request: BackendRequest) -> str:
        self.requests.append(request)
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.reply


class FakeAudio:
    def __init__(self, data: bytes = b"OggS-audio") -> None:
        self.data = data
        self.error: Exception | None = None
        self.urls: list[str] = []

    async def download_audio(self, url: str) -> bytes:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.data


class FakeRecognizer:
    def __init__(self, text: str = "напомни завтра позвонить Ивану") -> None:
        self.text = text
        self.error: Exception | None = None
        self.calls: list[tuple[bytes, str]] = []

    async def transcribe(self, audio: bytes, suffix: str) -> Transcript:
        self.calls.append((audio, suffix))
        if self.error is not None:
            raise self.error
        return Transcript(text=self.text, latency_s=0.1, audio_s=2.0)

    async def aclose(self) -> None:
        return None


class Collect:
    """Submit that keeps coroutines as tasks so tests can await them."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.tasks: list[asyncio.Task[None]] = []

    def __call__(self, work: Coroutine[object, object, None]) -> bool:
        if not self.accept:
            work.close()
            return False
        self.tasks.append(asyncio.create_task(work))
        return True

    async def drain(self) -> None:
        await asyncio.gather(*self.tasks)


def build_bot(**overrides: object) -> tuple[BotService, dict[str, object]]:
    parts: dict[str, object] = {
        "sender": FakeSender(),
        "gate": FakeGate(),
        "backend": FakeBackend(),
        "submit": Collect(),
        "audio": FakeAudio(),
        "recognizer": FakeRecognizer(),
        "timezone": "Europe/Moscow",
        "processing_timeout_s": 5.0,
        "clock": lambda: NOW,
    }
    parts.update(overrides)
    return BotService(**parts), parts  # type: ignore[arg-type]
