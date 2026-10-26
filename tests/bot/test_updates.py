from datetime import UTC, datetime

import pytest

from app.bot.updates import (
    AudioRef,
    BotStarted,
    Ignored,
    InvalidUpdateError,
    MessageCreated,
    has_meaningful_text,
    normalize,
    parse_update,
)


def message(body: object, *, sender: object | None = None) -> dict[str, object]:
    return {
        "update_type": "message_created",
        "timestamp": 1792920000000,
        "message": {
            "sender": sender
            if sender is not None
            else {"user_id": 42, "first_name": "Иван", "is_bot": False},
            "recipient": {"chat_id": 777, "chat_type": "dialog"},
            "timestamp": 1792920000000,
            "body": body,
        },
    }


def test_text_message() -> None:
    event = parse_update(message({"mid": "mid.1", "seq": 1, "text": "Создай задачу отчёт"}))
    assert event == MessageCreated(
        message_id="mid.1", chat_id="777", owner_id="42", text="Создай задачу отчёт", audio=None
    )


def test_voice_message_uses_audio_not_caption() -> None:
    body = {
        "mid": "mid.2",
        "text": "подпись",
        "attachments": [
            {"type": "image", "payload": {"url": "https://i.max.ru/1.jpg"}},
            {"type": "audio", "payload": {"url": "https://media.max.ru/v/1.ogg?x=1", "token": "t"}},
        ],
    }
    event = parse_update(message(body))
    assert isinstance(event, MessageCreated)
    assert event.text is None
    assert event.audio is not None
    assert event.audio.url == "https://media.max.ru/v/1.ogg?x=1"
    assert event.audio.suffix == ".ogg"


def test_message_without_text_or_audio() -> None:
    event = parse_update(
        message({"mid": "mid.3", "text": "  ", "attachments": [{"type": "sticker", "payload": {}}]})
    )
    assert isinstance(event, MessageCreated)
    assert event.text is None and event.audio is None


def test_bot_started() -> None:
    event = parse_update(
        {
            "update_type": "bot_started",
            "timestamp": 5,
            "chat_id": 777,
            "user": {"user_id": 42, "is_bot": False},
        }
    )
    assert event == BotStarted(event_id="max:bot_started:777:5", chat_id="777", owner_id="42")


@pytest.mark.parametrize(
    "raw",
    [
        {"update_type": "message_edited"},
        message({"mid": "m", "text": "hi"}, sender={"user_id": 1, "is_bot": True}),
        {**message(None)},
    ],
)
def test_ignored(raw: dict[str, object]) -> None:
    assert isinstance(parse_update(raw), Ignored)


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"timestamp": 1},
        message({"text": "без mid"}),
        message({"mid": "m", "text": 5}),
        message({"mid": "m", "attachments": ["not an object"]}),
        message({"mid": "m", "text": "x"}, sender={"user_id": True}),
        {"update_type": "bot_started", "chat_id": 1, "user": {"user_id": 2}},
    ],
)
def test_invalid(raw: object) -> None:
    with pytest.raises(InvalidUpdateError):
        parse_update(raw)


def test_normalize_fixes_received_at_and_identity() -> None:
    event = MessageCreated(message_id="mid.1", chat_id="777", owner_id="42", text="отчёт", audio=None)
    received = datetime(2026, 10, 25, 12, 30, tzinfo=UTC)
    first = normalize(event, timezone="Europe/Moscow", received_at=received)
    second = normalize(event, timezone="Europe/Moscow", received_at=received)
    assert first.source_event_id == second.source_event_id == "max:message:mid.1"
    assert first.request_id != second.request_id
    assert first.received_at == received and first.owner_id == "42"


def test_native_voice_without_message_is_ignored_with_reason() -> None:
    # What MAX currently sends for native voice messages from some app versions.
    raw = {"timestamp": 1784114462472, "user_locale": "ru", "update_type": "message_created"}
    assert parse_update(raw) == Ignored(update_type="message_created", reason="no_message")


@pytest.mark.parametrize(
    ("attachment", "expected"),
    [
        ({"type": "audio", "payload": {}}, AudioRef(url="", suffix="")),
        (
            {"type": "audio", "payload": {"url": "https://m.max.ru/x"}, "transcription": "позвони маме"},
            AudioRef(url="https://m.max.ru/x", suffix="", transcription="позвони маме"),
        ),
        (
            {"type": "file", "filename": "Запись.MP3", "payload": {"url": "https://f.max.ru/1"}},
            AudioRef(url="https://f.max.ru/1", suffix=".mp3"),
        ),
        ({"type": "file", "filename": "отчёт.pdf", "payload": {"url": "https://f.max.ru/2"}}, None),
    ],
)
def test_audio_attachments(attachment: dict[str, object], expected: AudioRef | None) -> None:
    event = parse_update(message({"mid": "m.a", "attachments": [attachment]}))
    assert isinstance(event, MessageCreated)
    assert event.audio == expected


@pytest.mark.parametrize(
    ("text", "meaningful"),
    [
        ("😀", False),
        ("😀😂👍🏽", False),
        ("👨‍👩‍👧 ❤️", False),
        ("  !!! ?? ", False),
        ("Создай задачу 😀", True),
        ("😀 в 10", True),
        ("ok", True),
    ],
)
def test_has_meaningful_text(text: str, meaningful: bool) -> None:
    assert has_meaningful_text(text) is meaningful
