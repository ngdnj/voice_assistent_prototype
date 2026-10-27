"""Local ASR first, cloud backup at peaks or on failure (app/asr/overflow.py)."""

import asyncio

import pytest
from pydantic import SecretStr

from app.asr.base import AsrError, AudioTooLongError, Transcript
from app.asr.factory import build_recognizer
from app.asr.overflow import OverflowRecognizer
from app.asr.yandex import normalize_times
from app.config import Settings


class FakeRecognizer:
    def __init__(self, name: str, *, fail: Exception | None = None) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0
        self.closed = False
        self.release = asyncio.Event()
        self.release.set()

    async def transcribe(self, audio: bytes, suffix: str) -> Transcript:
        self.calls += 1
        await self.release.wait()
        if self.fail is not None:
            raise self.fail
        return Transcript(text=self.name, latency_s=0.0)

    async def aclose(self) -> None:
        self.closed = True


async def test_backup_takes_messages_above_the_local_queue_limit() -> None:
    local, cloud = FakeRecognizer("local"), FakeRecognizer("cloud")
    local.release.clear()  # the local model is busy with the first two messages
    recognizer = OverflowRecognizer(local, cloud, max_in_primary=2)

    waiting = [asyncio.create_task(recognizer.transcribe(b"", ".wav")) for _ in range(2)]
    await asyncio.sleep(0)
    third = await recognizer.transcribe(b"", ".wav")
    local.release.set()

    assert third.text == "cloud"
    assert [t.text for t in await asyncio.gather(*waiting)] == ["local", "local"]
    # The queue is free again: the next message is local.
    assert (await recognizer.transcribe(b"", ".wav")).text == "local"
    assert (local.calls, cloud.calls) == (3, 1)


async def test_backup_takes_over_when_local_model_fails() -> None:
    local, cloud = FakeRecognizer("local", fail=AsrError("model crashed")), FakeRecognizer("cloud")
    recognizer = OverflowRecognizer(local, cloud, max_in_primary=4)
    assert (await recognizer.transcribe(b"", ".wav")).text == "cloud"
    assert recognizer._in_primary == 0  # a failure frees the slot


async def test_bad_input_is_not_sent_to_paid_backup() -> None:
    local, cloud = FakeRecognizer("local", fail=AudioTooLongError("30 s")), FakeRecognizer("cloud")
    recognizer = OverflowRecognizer(local, cloud, max_in_primary=4)
    with pytest.raises(AudioTooLongError):
        await recognizer.transcribe(b"", ".wav")
    assert cloud.calls == 0


async def test_close_closes_both() -> None:
    local, cloud = FakeRecognizer("local"), FakeRecognizer("cloud")
    await OverflowRecognizer(local, cloud, max_in_primary=1).aclose()
    assert local.closed and cloud.closed


def test_factory_builds_overflow_only_when_asked() -> None:
    base = Settings(_env_file=None, asr_backend="gigaam")
    assert not isinstance(build_recognizer(base), OverflowRecognizer)
    with_key = base.model_copy(update={"asr_overflow": "yandex", "yandex_api_key": SecretStr("k")})
    assert isinstance(build_recognizer(with_key), OverflowRecognizer)
    with pytest.raises(ValueError, match="YANDEX_API_KEY"):
        build_recognizer(base.model_copy(update={"asr_overflow": "yandex", "yandex_api_key": None}))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Real SpeechKit v1 answers of 26.10.
        ("Встреча с ольгой сергеевной 25 октября в 14 0 0", "Встреча с ольгой сергеевной 25 октября в 14:00"),
        (
            "Поставь сезон с командой альф в понедельник в 11 15",
            "Поставь сезон с командой альф в понедельник в 11:15",
        ),
        ("Напомни завтра в 9 30 позвонить ивану петрову", "Напомни завтра в 9:30 позвонить ивану петрову"),
        ("на субботу на 11 0 0", "на субботу на 11:00"),
        # Quantities and dates stay as they are.
        ("Купить 3 литра молока и 12 яиц", "Купить 3 литра молока и 12 яиц"),
        ("Перевести ане 2500 р до 10 числа", "Перевести ане 2500 р до 10 числа"),
        ("Сдать 2 книги до 30 октября", "Сдать 2 книги до 30 октября"),
    ],
)
def test_speechkit_times_are_joined(raw: str, expected: str) -> None:
    assert normalize_times(raw) == expected
