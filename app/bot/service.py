"""Bot scenario: welcome, one active request per user, text to backend, voice through ASR, reply.

The webhook path only deduplicates, takes the per-user lock, sends the "accepted" status and
hands the request to a runner; ASR and the backend run outside it (root AGENTS §11).
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from app.asr.audio import AudioDecodeError
from app.asr.base import AsrError, AudioTooLongError, SpeechRecognizer
from app.bot import messages, metrics
from app.bot.client import AudioFetchError, AudioSourceError, AudioTooLargeError, SendResult
from app.bot.gate import Gate
from app.bot.updates import (
    BotEvent,
    BotStarted,
    Ignored,
    IncomingMessage,
    MessageCreated,
    has_meaningful_text,
    normalize,
)

LOGGER = logging.getLogger("it_gorky.bot")

Outcome = Literal[
    "accepted", "busy", "welcomed", "unsupported", "invalid_input", "duplicate", "ignored", "rejected"
]


@dataclass(frozen=True, kw_only=True)
class BackendRequest:
    """What the backend (agent runner of role 1) needs to answer one request."""

    request_id: UUID
    job_id: str
    owner_id: str
    chat_id: str
    text: str
    reference_time: datetime  # received_at of the first admission; unchanged on retry
    timezone: str


class Backend(Protocol):
    async def answer(self, request: BackendRequest) -> str:
        """Return the reply text for the user; raise on failure (the bot answers FAILED)."""
        ...


class Sender(Protocol):
    async def send_text(self, *, chat_id: str, text: str) -> SendResult: ...

    async def edit_text(self, *, message_id: str, text: str) -> SendResult: ...


class AudioSource(Protocol):
    async def download_audio(self, url: str) -> bytes: ...


Submit = Callable[[Coroutine[object, object, None]], bool]


def log_event(event: str, **ids: str | UUID) -> None:
    """Only fixed event names and correlation ids; never texts, audio or exceptions."""
    LOGGER.info(json.dumps({"event": event, **{key: str(value) for key, value in ids.items()}}))


class BackgroundRunner:
    """Bounded in-process runner. Not durable: a crash loses work, the lock then expires by TTL."""

    def __init__(self, *, max_concurrency: int) -> None:
        self._limit = max_concurrency
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    def submit(self, work: Coroutine[object, object, None]) -> bool:
        if self._closing or len(self._tasks) >= self._limit:
            work.close()
            return False
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    @property
    def active(self) -> int:
        return len(self._tasks)

    async def aclose(self, timeout_s: float) -> None:
        """Stop admission, wait for active work within a budget, then cancel the rest."""
        self._closing = True
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=timeout_s)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


class BotService:
    def __init__(
        self,
        *,
        sender: Sender,
        gate: Gate,
        backend: Backend,
        submit: Submit,
        audio: AudioSource,
        recognizer: SpeechRecognizer | None,
        timezone: str,
        processing_timeout_s: float,
        clock: Callable[[], datetime],
        allowed_chat_id: str | None = None,
    ) -> None:
        self._sender = sender
        self._gate = gate
        self._backend = backend
        self._submit = submit
        self._audio = audio
        self._recognizer = recognizer
        self._timezone = timezone
        self._timeout = processing_timeout_s
        self._clock = clock
        self._allowed_chat_id = allowed_chat_id

    async def handle(self, event: BotEvent) -> Outcome:
        """Handle one parsed update. Redis errors propagate so the webhook is redelivered."""
        if isinstance(event, Ignored):
            if event.reason == "no_message":
                # Nothing identifies the user or chat, so no answer is possible (INTEGRATION.md §10).
                metrics.UPDATES.labels("other", "no_payload").inc()
                log_event("message_without_payload")
            else:
                metrics.UPDATES.labels("other", "ignored").inc()
            return "ignored"
        if self._allowed_chat_id is not None and event.chat_id != self._allowed_chat_id:
            metrics.UPDATES.labels("other", "ignored").inc()
            return "ignored"
        if isinstance(event, BotStarted):
            kind, event_id = "start", event.event_id
        else:
            kind = "audio" if event.audio is not None else "text" if event.text is not None else "other"
            event_id = f"max:message:{event.message_id}"
        if not await self._gate.first_seen(event_id):
            metrics.UPDATES.labels(kind, "duplicate").inc()
            return "duplicate"
        try:
            outcome = await self._dispatch(event)
        except BaseException:
            # Nothing was admitted: let MAX redeliver the same event.
            await asyncio.shield(self._forget(event_id))
            metrics.UPDATES.labels(kind, "error").inc()
            raise
        metrics.UPDATES.labels(kind, outcome).inc()
        return outcome

    async def _forget(self, event_id: str) -> None:
        try:
            await self._gate.forget(event_id)
        except Exception:
            log_event("dedup_forget_failed")

    async def _dispatch(self, event: BotStarted | MessageCreated) -> Outcome:
        if isinstance(event, BotStarted):
            return await self._welcome(event.owner_id, event.chat_id)
        if event.text is not None and event.text.strip().lower() in messages.START_COMMANDS:
            return await self._welcome(event.owner_id, event.chat_id)
        if event.text is not None and not has_meaningful_text(event.text):
            if await self._gate.is_busy(event.owner_id):
                await self._send(event.chat_id, messages.BUSY, "busy")
                return "busy"
            await self._send(event.chat_id, messages.INVALID_REQUEST, "error")
            return "invalid_input"
        if event.text is None and event.audio is None:
            if await self._gate.is_busy(event.owner_id):
                await self._send(event.chat_id, messages.BUSY, "busy")
                return "busy"
            await self._send(event.chat_id, messages.UNSUPPORTED, "error")
            return "unsupported"

        incoming = normalize(event, timezone=self._timezone, received_at=self._clock())
        job_id = str(uuid4())
        if not await self._gate.acquire(incoming.owner_id, job_id):
            await self._send(incoming.chat_id, messages.BUSY, "busy")
            log_event("busy", request_id=incoming.request_id)
            return "busy"
        try:
            status = await self._send(incoming.chat_id, messages.ACCEPTED, "status")
            status_id = status.external_message_id if status.ok else ""
            admitted = self._submit(self._process(incoming, job_id, status_id))
        except BaseException:
            await asyncio.shield(self._release(incoming.owner_id, job_id))
            raise
        if not admitted:
            await self._release(incoming.owner_id, job_id)
            await self._finish_reply(incoming.chat_id, status_id, messages.NOT_ACCEPTED, "error")
            log_event("rejected", request_id=incoming.request_id)
            return "rejected"
        log_event("accepted", request_id=incoming.request_id, job_id=job_id)
        return "accepted"

    async def _welcome(self, owner_id: str, chat_id: str) -> Outcome:
        # "Other functions" are unavailable while a request is processed, including the greeting.
        if await self._gate.is_busy(owner_id):
            await self._send(chat_id, messages.BUSY, "busy")
            return "busy"
        await self._send(chat_id, messages.WELCOME, "welcome")
        return "welcomed"

    async def _send(self, chat_id: str, text: str, purpose: str) -> SendResult:
        started = time.monotonic()
        result = await self._sender.send_text(chat_id=chat_id, text=text)
        metrics.SEND_SECONDS.labels(purpose).observe(time.monotonic() - started)
        metrics.SENDS.labels(purpose, result.outcome).inc()
        if not result.ok:
            log_event("send_failed", purpose=purpose, outcome=result.outcome, error_code=result.error_code)
        return result

    async def _release(self, owner_id: str, job_id: str) -> None:
        try:
            if not await self._gate.release(owner_id, job_id):
                log_event("lock_already_released", job_id=job_id)
        except Exception:
            # The lock still expires by its TTL; the user is blocked at most BOT_BUSY_TTL_S.
            log_event("lock_release_failed", job_id=job_id)

    async def _finish_reply(self, chat_id: str, status_id: str, text: str, purpose: str) -> None:
        if status_id:
            started = time.monotonic()
            try:
                result = await self._sender.edit_text(message_id=status_id, text=text)
            except Exception:
                # Editing must never repeat an already completed backend operation.
                result = SendResult("retryable_failure", error_code="edit_failed")
            metrics.SEND_SECONDS.labels("edit").observe(time.monotonic() - started)
            metrics.SENDS.labels("edit", result.outcome).inc()
            if result.ok:
                return
            log_event("edit_failed", outcome=result.outcome, error_code=result.error_code)
        # Missing status ID or rejected edit: preserve the answer via an ordinary send.
        await self._send(chat_id, text, purpose)

    async def _process(self, incoming: IncomingMessage, job_id: str, status_id: str = "") -> None:
        ids: dict[str, str | UUID] = {"request_id": incoming.request_id, "job_id": job_id}
        reply: str | None = None
        outcome = "failed"
        try:
            try:
                async with asyncio.timeout(self._timeout):
                    reply, outcome = await self._prepare_reply(incoming, job_id)
            except TimeoutError:
                reply, outcome = messages.FAILED, "timeout"
            except Exception:
                reply, outcome = messages.FAILED, "failed"
            log_event(f"processing_{outcome}", **ids)
            if reply:
                await self._finish_reply(
                    incoming.chat_id, status_id, reply, "reply" if outcome == "replied" else "error"
                )
        except asyncio.CancelledError:
            outcome = "cancelled"
            log_event("processing_cancelled", **ids)
            raise
        finally:
            metrics.PROCESSING.labels(outcome).inc()
            await asyncio.shield(self._release(incoming.owner_id, job_id))

    async def _prepare_reply(self, incoming: IncomingMessage, job_id: str) -> tuple[str, str]:
        text = incoming.text
        if incoming.audio_ref is not None:
            recognized, outcome = await self._recognize(incoming)
            if recognized is None:
                return outcome
            await self._send(incoming.chat_id, messages.TRANSCRIPT.format(text=recognized), "transcript")
            text = recognized
        if text is None:
            raise ValueError("Message has no text to process")
        reply = await self._backend.answer(
            BackendRequest(
                request_id=incoming.request_id,
                job_id=job_id,
                owner_id=incoming.owner_id,
                chat_id=incoming.chat_id,
                text=text,
                reference_time=incoming.received_at,
                timezone=incoming.timezone,
            )
        )
        if not reply.strip():
            return messages.FAILED, "failed"
        return reply, "replied"

    async def _recognize(self, incoming: IncomingMessage) -> tuple[str | None, tuple[str, str]]:
        """Return recognized text, or None and the (reply, outcome) to send instead.

        Our ASR (role 3) is the primary path. If it is not configured or fails, a transcription
        delivered by MAX in the attachment is used instead; otherwise the user gets a clear refusal.
        """
        ref = incoming.audio_ref
        if ref is None:
            return None, (messages.AUDIO_FAILED, "asr_error")
        failure: tuple[str, str]
        if not ref.url:
            log_event("audio_without_url", request_id=incoming.request_id)
            failure = (messages.AUDIO_UNREADABLE, "audio_error")
        elif self._recognizer is None:
            log_event("asr_not_configured", request_id=incoming.request_id)
            failure = (messages.AUDIO_FAILED, "asr_error")
        else:
            text, failure = await self._transcribe(incoming, ref.url, ref.suffix, self._recognizer)
            if text is not None:
                return text, ("", "")
        # The fallback covers a missing URL or ASR, never size/length limits or silence.
        fallback_allowed = not ref.url or failure[1] == "asr_error"
        if ref.transcription and fallback_allowed:
            log_event("asr_max_transcription_used", request_id=incoming.request_id)
            return ref.transcription.strip(), ("", "")
        return None, failure

    async def _transcribe(
        self, incoming: IncomingMessage, url: str, suffix: str, recognizer: SpeechRecognizer
    ) -> tuple[str | None, tuple[str, str]]:
        try:
            audio = await self._audio.download_audio(url)
        except AudioTooLargeError:
            metrics.AUDIO_FETCH.labels("too_large").inc()
            return None, (messages.AUDIO_TOO_LARGE, "audio_error")
        except AudioSourceError as error:
            metrics.AUDIO_FETCH.labels("source_error").inc()
            # The host helps to fill MAX_AUDIO_HOSTS; it is not user content.
            log_event("audio_source_rejected", request_id=incoming.request_id, host=error.host)
            return None, (messages.AUDIO_UNREADABLE, "audio_error")
        except AudioFetchError as error:
            metrics.AUDIO_FETCH.labels("fetch_error").inc()
            # Fixed texts with an HTTP status at most; never the URL.
            log_event("audio_fetch_failed", request_id=incoming.request_id, reason=str(error))
            return None, (messages.AUDIO_FAILED, "asr_error")
        metrics.AUDIO_FETCH.labels("ok").inc()
        try:
            transcript = await recognizer.transcribe(audio, suffix)
        except AudioTooLongError:
            return None, (messages.AUDIO_TOO_LONG, "audio_error")
        except AudioDecodeError:
            return None, (messages.AUDIO_UNREADABLE, "audio_error")
        except AsrError:
            log_event("asr_failed", request_id=incoming.request_id)
            return None, (messages.AUDIO_FAILED, "asr_error")
        text = transcript.text.strip()
        if not text:
            return None, (messages.NOT_HEARD, "not_heard")
        return text, ("", "")
