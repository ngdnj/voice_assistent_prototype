"""Composition and lifecycle of the currently implemented local backend."""

import asyncio
import json
import logging
import math
import os
import signal
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis

from app.planner.meeting_service import MeetingService
from app.planner.reminder_service import ReminderService
from app.planner.service import TaskService
from app.storage.notification_stream import NotificationStream
from app.storage.redis import RedisSettings, redis_connection
from app.storage.redis_meetings import RedisMeetingRepository
from app.storage.redis_reminders import RedisReminderRepository
from app.storage.redis_tasks import RedisTaskRepository

LOGGER = logging.getLogger("it_gorky.runtime")


@dataclass(frozen=True)
class RuntimeSettings:
    redis: RedisSettings
    shutdown_timeout: float = 5.0
    demo_owner_id: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.shutdown_timeout) or self.shutdown_timeout <= 0:
            raise ValueError("BACKEND_SHUTDOWN_TIMEOUT must be finite and positive")

    @classmethod
    def from_env(cls, *, require_demo_owner: bool = False) -> "RuntimeSettings":
        owner = os.environ.get("BACKEND_DEMO_OWNER_ID", "").strip()
        if require_demo_owner and not owner:
            raise ValueError("BACKEND_DEMO_OWNER_ID is required for the local example")
        return cls(
            redis=RedisSettings.from_env(),
            shutdown_timeout=float(os.environ.get("BACKEND_SHUTDOWN_TIMEOUT", "5")),
            demo_owner_id=owner or None,
        )


@dataclass(frozen=True)
class Backend:
    tasks: TaskService
    meetings: MeetingService
    reminders: ReminderService
    notifications: NotificationStream


def build_backend(client: Redis, *, clock: Callable[[], datetime]) -> Backend:
    """One shared pool; providers and transports are deliberately not started."""
    return Backend(
        tasks=TaskService(repository=RedisTaskRepository(client), clock=clock),
        meetings=MeetingService(repository=RedisMeetingRepository(client), clock=clock),
        reminders=ReminderService(repository=RedisReminderRepository(client), clock=clock),
        notifications=NotificationStream(client),
    )


def log_event(event: str, *, request_id: UUID, operation_id: UUID | None = None) -> None:
    """Only fixed events and typed correlation IDs; never log payloads or exceptions."""
    fields = {"event": event, "request_id": str(request_id)}
    if operation_id is not None:
        fields["operation_id"] = str(operation_id)
    LOGGER.info(json.dumps(fields))


async def run_backend(
    settings: RuntimeSettings,
    work: Callable[[Backend], Coroutine[object, object, None]],
    *,
    request_id: UUID,
    operation_id: UUID | None = None,
) -> int:
    """Run one admitted operation, draining it on SIGINT/SIGTERM within a budget.

    There is no queue reader yet. Future readers must stop admission on the same
    stop signal; this runner does not acknowledge jobs or delivery messages.
    Work must cooperate with asyncio cancellation and use bounded I/O.
    """
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    installed: list[signal.Signals] = []
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def emit(event: str) -> None:
        log_event(event, request_id=request_id, operation_id=operation_id)

    def stop() -> None:
        if not stopping.is_set():
            emit("shutdown_requested")
            stopping.set()

    waiter: asyncio.Task[bool] | None = None
    operation: asyncio.Task[None] | None = None
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop)
            installed.append(sig)
        emit("starting")
        async with redis_connection(settings.redis) as client:
            if stopping.is_set():
                return 130
            backend = build_backend(client, clock=lambda: datetime.now(UTC))
            emit("ready")
            operation = asyncio.create_task(work(backend))
            waiter = asyncio.create_task(stopping.wait())
            try:
                done, _ = await asyncio.wait((operation, waiter), return_when=asyncio.FIRST_COMPLETED)
                if operation not in done:
                    emit("draining")
                    finished, _ = await asyncio.wait((operation,), timeout=settings.shutdown_timeout)
                    if not finished:
                        operation.cancel()
                        await asyncio.gather(operation, return_exceptions=True)
                        emit("work_interrupted")
                        return 130
                await operation  # Errors must not be reported as success.
                emit("work_completed")
                return 0
            finally:
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
    finally:
        # Also close resources if the caller cancels this runner itself.
        for task in (operation, waiter):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (operation, waiter) if task is not None), return_exceptions=True
        )
        for sig in installed:
            loop.remove_signal_handler(sig)
            signal.signal(sig, previous_handlers[sig])
        emit("stopped")
