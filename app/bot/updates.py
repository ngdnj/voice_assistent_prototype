"""MAX updates -> typed bot events -> IncomingMessage (INTEGRATION.md §2).

Field names follow the MAX Bot API objects Update, Message, User
(https://dev.max.ru/docs-api/objects/Update). Unknown update types are not errors:
the bot ignores them. Anything malformed is rejected before any side effect.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

# Containers ffmpeg reads reliably from a temporary file name; others are probed by content.
AUDIO_SUFFIXES = frozenset({".ogg", ".oga", ".opus", ".mp3", ".m4a", ".mp4", ".aac", ".wav", ".webm"})
MAX_ID_LENGTH = 200


class InvalidUpdateError(ValueError):
    """The update does not match the expected schema; the reason is not shown to clients."""


@dataclass(frozen=True, kw_only=True)
class AudioRef:
    url: str  # "" when MAX sent the attachment without a download URL
    suffix: str
    # Text recognized by MAX itself, if the attachment carries it; a fallback for our ASR.
    transcription: str | None = None


@dataclass(frozen=True, kw_only=True)
class BotStarted:
    event_id: str
    chat_id: str
    owner_id: str


@dataclass(frozen=True, kw_only=True)
class MessageCreated:
    message_id: str
    chat_id: str
    owner_id: str
    text: str | None
    audio: AudioRef | None


@dataclass(frozen=True, kw_only=True)
class Ignored:
    update_type: str
    # "no_message": message_created without a message object. MAX currently sends such
    # empty updates for native voice messages from some app versions; there is no chat to answer.
    reason: str = ""


BotEvent = BotStarted | MessageCreated | Ignored


@dataclass(frozen=True, kw_only=True)
class IncomingMessage:
    """Internal normalized message (root AGENTS §8); not the MAX wire format."""

    request_id: UUID
    source_event_id: str
    channel: Literal["max"]
    owner_id: str
    chat_id: str
    received_at: datetime
    timezone: str
    text: str | None
    audio_ref: AudioRef | None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.audio_ref is None):
            raise ValueError("Exactly one of text and audio_ref is required")
        if self.received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidUpdateError(f"{name} must be an object")
    return value


def _id(value: object, name: str) -> str:
    # MAX ids are int64; strings are accepted only for message ids (mid).
    if type(value) is int and value != 0:
        return str(value)
    if isinstance(value, str) and value.strip() and len(value) <= MAX_ID_LENGTH:
        return value
    raise InvalidUpdateError(f"{name} is invalid")


def _suffix(*names: object) -> str:
    for name in names:
        if isinstance(name, str) and name:
            suffix = PurePosixPath(urlsplit(name).path).suffix.lower()
            if suffix in AUDIO_SUFFIXES:
                return suffix
    return ""


def _audio(attachments: object) -> AudioRef | None:
    """Voice/audio attachment, or an audio file sent as a document (`file` with an audio extension)."""
    if attachments is None:
        return None
    if not isinstance(attachments, list):
        raise InvalidUpdateError("attachments must be a list")
    for item in attachments:
        attachment = _object(item, "attachment")
        kind = attachment.get("type")
        if kind not in ("audio", "file"):
            continue
        payload = attachment.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        url = payload.get("url")
        url = url if isinstance(url, str) else ""
        suffix = _suffix(attachment.get("filename"), payload.get("filename"), url)
        if kind == "file" and not suffix:
            continue  # A document that is not audio.
        transcription = attachment.get("transcription")
        return AudioRef(
            url=url,
            suffix=suffix,
            transcription=transcription if isinstance(transcription, str) and transcription.strip() else None,
        )
    return None


def has_meaningful_text(text: str) -> bool:
    """True if the text has a letter or digit: emoji, punctuation and spaces alone are not a request."""
    return any(char.isalnum() for char in text)


def parse_update(raw: object) -> BotEvent:
    update = _object(raw, "update")
    update_type = update.get("update_type")
    if not isinstance(update_type, str) or not update_type:
        raise InvalidUpdateError("update_type is missing")
    if update_type == "bot_started":
        user = _object(update.get("user"), "user")
        if user.get("is_bot") is True:
            return Ignored(update_type=update_type)
        chat_id = _id(update.get("chat_id"), "chat_id")
        timestamp = update.get("timestamp")
        if type(timestamp) is not int:
            raise InvalidUpdateError("timestamp is invalid")
        return BotStarted(
            event_id=f"max:bot_started:{chat_id}:{timestamp}",
            chat_id=chat_id,
            owner_id=_id(user.get("user_id"), "user_id"),
        )
    if update_type != "message_created":
        return Ignored(update_type=update_type)

    if update.get("message") is None:
        return Ignored(update_type=update_type, reason="no_message")
    message = _object(update.get("message"), "message")
    sender = message.get("sender")
    # Channel posts have no sender; bots (including this one) are never treated as users.
    if sender is None or _object(sender, "sender").get("is_bot") is True:
        return Ignored(update_type=update_type)
    owner_id = _id(_object(sender, "sender").get("user_id"), "user_id")
    chat_id = _id(_object(message.get("recipient"), "recipient").get("chat_id"), "chat_id")
    body = message.get("body")
    if body is None:  # Only a forwarded message: nothing to process.
        return Ignored(update_type=update_type)
    body_object = _object(body, "body")
    message_id = _id(body_object.get("mid"), "mid")
    text = body_object.get("text")
    if text is not None and not isinstance(text, str):
        raise InvalidUpdateError("text must be a string")
    audio = _audio(body_object.get("attachments"))
    if audio is not None:
        text = None  # A voice message is processed by its audio, not by a caption.
    elif text is None or not text.strip():
        text = None
    return MessageCreated(message_id=message_id, chat_id=chat_id, owner_id=owner_id, text=text, audio=audio)


def normalize(
    event: MessageCreated, *, timezone: str, received_at: datetime | None = None
) -> IncomingMessage:
    """Fix received_at once, at the first admission of the event."""
    if event.text is None and event.audio is None:
        raise ValueError("Event has neither text nor audio")
    return IncomingMessage(
        request_id=uuid4(),
        source_event_id=f"max:message:{event.message_id}",
        channel="max",
        owner_id=event.owner_id,
        chat_id=event.chat_id,
        received_at=datetime.now(UTC) if received_at is None else received_at.astimezone(UTC),
        timezone=timezone,
        text=event.text,
        audio_ref=event.audio,
    )
