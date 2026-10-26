import asyncio

import pytest

from app.asr.audio import AudioDecodeError
from app.asr.base import AsrError, AudioTooLongError
from app.bot import messages
from app.bot.client import AudioFetchError, AudioSourceError, AudioTooLargeError, SendResult
from app.bot.service import BackgroundRunner
from app.bot.updates import AudioRef, BotStarted, Ignored, MessageCreated
from tests.bot.fakes import (
    NOW,
    Collect,
    FakeAudio,
    FakeBackend,
    FakeGate,
    FakeRecognizer,
    FakeSender,
    build_bot,
)

TEXT = MessageCreated(
    message_id="mid.1", chat_id="777", owner_id="42", text="Создай задачу отчёт", audio=None
)
VOICE = MessageCreated(
    message_id="mid.2",
    chat_id="777",
    owner_id="42",
    text=None,
    audio=AudioRef(url="https://media.max.ru/a.ogg", suffix=".ogg"),
)
START = BotStarted(event_id="max:bot_started:777:1", chat_id="777", owner_id="42")


async def test_first_start_sends_welcome() -> None:
    bot, parts = build_bot()
    assert await bot.handle(START) == "welcomed"
    assert parts["sender"].texts == [messages.WELCOME]  # type: ignore[attr-defined]


async def test_text_goes_to_backend_and_reply_is_shown() -> None:
    bot, parts = build_bot()
    sender, gate, backend, submit = parts["sender"], parts["gate"], parts["backend"], parts["submit"]
    assert await bot.handle(TEXT) == "accepted"
    assert gate.locks  # type: ignore[attr-defined]
    await submit.drain()  # type: ignore[attr-defined]
    assert sender.texts == [messages.ACCEPTED, "Записал задачу «Отчёт»"]  # type: ignore[attr-defined]
    request = backend.requests[0]  # type: ignore[attr-defined]
    assert (request.owner_id, request.text, request.timezone) == (
        "42",
        "Создай задачу отчёт",
        "Europe/Moscow",
    )
    assert request.reference_time == NOW
    assert not gate.locks  # type: ignore[attr-defined]


async def test_voice_goes_through_asr_and_transcript_is_shown() -> None:
    bot, parts = build_bot()
    await bot.handle(VOICE)
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["sender"].texts == [  # type: ignore[attr-defined]
        messages.ACCEPTED,
        "Распознано: напомни завтра позвонить Ивану",
        "Записал задачу «Отчёт»",
    ]
    assert parts["recognizer"].calls == [(b"OggS-audio", ".ogg")]  # type: ignore[attr-defined]
    assert parts["backend"].requests[0].text == "напомни завтра позвонить Ивану"  # type: ignore[attr-defined]


async def test_any_input_while_processing_gets_busy_message() -> None:
    backend = FakeBackend()
    backend.release.clear()
    bot, parts = build_bot(backend=backend)
    sender, submit = parts["sender"], parts["submit"]
    assert await bot.handle(TEXT) == "accepted"
    await asyncio.sleep(0)
    second = MessageCreated(message_id="mid.9", chat_id="777", owner_id="42", text="ещё", audio=None)
    sticker = MessageCreated(message_id="mid.10", chat_id="777", owner_id="42", text=None, audio=None)
    command = MessageCreated(message_id="mid.11", chat_id="777", owner_id="42", text="/start", audio=None)
    assert await bot.handle(second) == "busy"
    assert await bot.handle(VOICE) == "busy"
    assert await bot.handle(sticker) == "busy"
    assert await bot.handle(command) == "busy"
    assert await bot.handle(START) == "busy"
    assert len(backend.requests) == 1  # No second processing chain.
    backend.release.set()
    await submit.drain()  # type: ignore[attr-defined]
    assert sender.texts.count(messages.BUSY) == 5  # type: ignore[attr-defined]
    # After the reply the user can send a new request.
    assert (
        await bot.handle(
            MessageCreated(message_id="mid.12", chat_id="777", owner_id="42", text="ок", audio=None)
        )
        == "accepted"
    )


async def test_other_users_are_independent() -> None:
    backend = FakeBackend()
    backend.release.clear()
    bot, parts = build_bot(backend=backend)
    await bot.handle(TEXT)
    other = MessageCreated(message_id="mid.20", chat_id="888", owner_id="43", text="встреча", audio=None)
    assert await bot.handle(other) == "accepted"
    backend.release.set()
    await parts["submit"].drain()  # type: ignore[attr-defined]


async def test_redelivered_event_is_processed_once() -> None:
    bot, parts = build_bot()
    assert await bot.handle(TEXT) == "accepted"
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert await bot.handle(TEXT) == "duplicate"
    assert len(parts["backend"].requests) == 1  # type: ignore[attr-defined]


async def test_ignored_and_unsupported() -> None:
    bot, parts = build_bot()
    assert await bot.handle(Ignored(update_type="message_edited")) == "ignored"
    sticker = MessageCreated(message_id="mid.3", chat_id="777", owner_id="42", text=None, audio=None)
    assert await bot.handle(sticker) == "unsupported"
    assert parts["sender"].texts == [messages.UNSUPPORTED]  # type: ignore[attr-defined]


async def test_backend_failure_gives_honest_error_and_releases_lock() -> None:
    backend = FakeBackend()
    backend.error = RuntimeError("llm down")
    bot, parts = build_bot(backend=backend)
    await bot.handle(TEXT)
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["sender"].texts[-1] == messages.FAILED  # type: ignore[attr-defined]
    assert not parts["gate"].locks  # type: ignore[attr-defined]


async def test_processing_timeout() -> None:
    backend = FakeBackend()
    backend.release.clear()
    bot, parts = build_bot(backend=backend, processing_timeout_s=0.05)
    await bot.handle(TEXT)
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["sender"].texts[-1] == messages.FAILED  # type: ignore[attr-defined]
    assert not parts["gate"].locks  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("where", "error", "reply"),
    [
        ("audio", AudioTooLargeError(), messages.AUDIO_TOO_LARGE),
        ("audio", AudioSourceError(), messages.AUDIO_UNREADABLE),
        ("audio", AudioFetchError(), messages.AUDIO_FAILED),
        ("asr", AudioTooLongError(), messages.AUDIO_TOO_LONG),
        ("asr", AudioDecodeError(), messages.AUDIO_UNREADABLE),
        ("asr", AsrError("http 500"), messages.AUDIO_FAILED),
    ],
)
async def test_voice_errors_do_not_reach_backend(where: str, error: Exception, reply: str) -> None:
    audio, recognizer = FakeAudio(), FakeRecognizer()
    (audio if where == "audio" else recognizer).error = error
    bot, parts = build_bot(audio=audio, recognizer=recognizer)
    await bot.handle(VOICE)
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["sender"].texts == [messages.ACCEPTED, reply]  # type: ignore[attr-defined]
    assert not parts["backend"].requests  # type: ignore[attr-defined]
    assert not parts["gate"].locks  # type: ignore[attr-defined]


async def test_silence_is_not_sent_to_backend() -> None:
    bot, parts = build_bot(recognizer=FakeRecognizer(text="  "))
    await bot.handle(VOICE)
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["sender"].texts == [messages.ACCEPTED, messages.NOT_HEARD]  # type: ignore[attr-defined]


async def test_voice_without_recognizer() -> None:
    bot, parts = build_bot(recognizer=None)
    await bot.handle(VOICE)
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["sender"].texts[-1] == messages.AUDIO_FAILED  # type: ignore[attr-defined]


async def test_runner_full_rejects_and_releases_lock() -> None:
    bot, parts = build_bot(submit=Collect(accept=False))
    assert await bot.handle(TEXT) == "rejected"
    assert parts["sender"].texts == [messages.ACCEPTED, messages.NOT_ACCEPTED]  # type: ignore[attr-defined]
    assert not parts["gate"].locks  # type: ignore[attr-defined]


async def test_storage_error_propagates_and_event_can_be_redelivered() -> None:
    gate = FakeGate()
    bot, _ = build_bot(gate=gate)

    async def broken(owner_id: str, job_id: str) -> bool:
        raise ConnectionError("redis down")

    gate.acquire = broken  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await bot.handle(TEXT)
    assert "max:message:mid.1" not in gate.events


async def test_failed_status_send_does_not_stop_processing() -> None:
    sender = FakeSender()
    sender.result = SendResult("unknown", error_code="timeout")
    bot, parts = build_bot(sender=sender)
    assert await bot.handle(TEXT) == "accepted"
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert len(parts["backend"].requests) == 1  # type: ignore[attr-defined]
    assert not parts["gate"].locks  # type: ignore[attr-defined]


async def test_background_runner_limits_and_drains() -> None:
    runner = BackgroundRunner(max_concurrency=1)
    gate = asyncio.Event()
    done: list[str] = []

    async def work(name: str) -> None:
        await gate.wait()
        done.append(name)

    assert runner.submit(work("a"))
    assert not runner.submit(work("b"))
    gate.set()
    await runner.aclose(1.0)
    assert done == ["a"]
    assert not runner.submit(work("c"))


async def test_shutdown_cancels_and_releases_lock() -> None:
    backend = FakeBackend()
    backend.release.clear()
    runner = BackgroundRunner(max_concurrency=2)
    bot, parts = build_bot(backend=backend, submit=runner.submit)
    await bot.handle(TEXT)
    await asyncio.sleep(0)
    await runner.aclose(0.01)
    assert not parts["gate"].locks  # type: ignore[attr-defined]


def voice(ref: AudioRef, mid: str = "mid.v") -> MessageCreated:
    return MessageCreated(message_id=mid, chat_id="777", owner_id="42", text=None, audio=ref)


async def test_emoji_only_is_an_invalid_request() -> None:
    bot, parts = build_bot()
    emoji = MessageCreated(message_id="mid.e", chat_id="777", owner_id="42", text="😀👍", audio=None)
    assert await bot.handle(emoji) == "invalid_input"
    assert parts["sender"].texts == [messages.INVALID_REQUEST]  # type: ignore[attr-defined]
    assert not parts["gate"].locks and not parts["backend"].requests  # type: ignore[attr-defined]


async def test_emoji_inside_text_is_processed() -> None:
    bot, parts = build_bot()
    mixed = MessageCreated(
        message_id="mid.m", chat_id="777", owner_id="42", text="Отчёт к пятнице 🔥", audio=None
    )
    assert await bot.handle(mixed) == "accepted"
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["backend"].requests[0].text == "Отчёт к пятнице 🔥"  # type: ignore[attr-defined]


async def test_emoji_while_busy_gets_busy_message() -> None:
    backend = FakeBackend()
    backend.release.clear()
    bot, parts = build_bot(backend=backend)
    await bot.handle(TEXT)
    emoji = MessageCreated(message_id="mid.e2", chat_id="777", owner_id="42", text="😀", audio=None)
    assert await bot.handle(emoji) == "busy"
    backend.release.set()
    await parts["submit"].drain()  # type: ignore[attr-defined]


async def test_empty_voice_update_is_logged_not_answered() -> None:
    bot, parts = build_bot()
    assert await bot.handle(Ignored(update_type="message_created", reason="no_message")) == "ignored"
    assert parts["sender"].texts == []  # type: ignore[attr-defined]


async def test_max_transcription_is_used_without_our_asr() -> None:
    bot, parts = build_bot(recognizer=None)
    ref = AudioRef(url="https://media.max.ru/a.ogg", suffix=".ogg", transcription="позвони маме")
    await bot.handle(voice(ref))
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["sender"].texts[1] == "Распознано: позвони маме"  # type: ignore[attr-defined]
    assert parts["backend"].requests[0].text == "позвони маме"  # type: ignore[attr-defined]
    assert parts["audio"].urls == []  # type: ignore[attr-defined]


async def test_max_transcription_is_used_when_url_is_missing() -> None:
    bot, parts = build_bot()
    await bot.handle(voice(AudioRef(url="", suffix="", transcription="встреча в 15")))
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["backend"].requests[0].text == "встреча в 15"  # type: ignore[attr-defined]


async def test_voice_without_url_and_transcription() -> None:
    bot, parts = build_bot()
    await bot.handle(voice(AudioRef(url="", suffix="")))
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["sender"].texts == [messages.ACCEPTED, messages.AUDIO_UNREADABLE]  # type: ignore[attr-defined]
    assert not parts["gate"].locks  # type: ignore[attr-defined]


async def test_asr_failure_falls_back_to_max_transcription() -> None:
    recognizer = FakeRecognizer()
    recognizer.error = AsrError("http 500")
    bot, parts = build_bot(recognizer=recognizer)
    await bot.handle(voice(AudioRef(url="https://media.max.ru/a.ogg", suffix=".ogg", transcription="отчёт")))
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["backend"].requests[0].text == "отчёт"  # type: ignore[attr-defined]


async def test_limits_are_not_bypassed_by_transcription() -> None:
    audio = FakeAudio()
    audio.error = AudioTooLargeError()
    bot, parts = build_bot(audio=audio)
    await bot.handle(voice(AudioRef(url="https://media.max.ru/a.ogg", suffix=".ogg", transcription="x")))
    await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["sender"].texts[-1] == messages.AUDIO_TOO_LARGE  # type: ignore[attr-defined]
    assert not parts["backend"].requests  # type: ignore[attr-defined]
