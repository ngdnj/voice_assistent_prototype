"""Long polling loop for local development without a public HTTPS webhook.

A batch is confirmed (the next marker is used) only after every update in it was handled;
on a storage error the same batch is fetched again and already handled events are skipped
by the event deduplication of the gate.
"""

import asyncio
import contextlib

from app.bot.client import MaxClient, PollingError
from app.bot.service import BotService, log_event
from app.bot.updates import InvalidUpdateError, parse_update

UPDATE_TYPES = ("bot_started", "message_created")
RETRY_DELAY_S = 5.0


async def run_polling(
    client: MaxClient, bot: BotService, stop: asyncio.Event, *, timeout_s: int = 30
) -> None:
    marker: int | None = None
    while not stop.is_set():
        try:
            updates, next_marker = await client.get_updates(
                marker=marker, timeout_s=timeout_s, types=UPDATE_TYPES
            )
            for raw in updates:
                try:
                    event = parse_update(raw)
                except InvalidUpdateError as error:
                    # Reasons are fixed schema messages, never values from the update.
                    log_event("polling_invalid_update", reason=str(error))
                    continue
                await bot.handle(event)
        except PollingError as error:
            log_event("polling_failed", error_code=error.code)
        except Exception:
            # Redis or another dependency failed: keep the marker and refetch the batch later.
            log_event("polling_handle_failed")
        else:
            marker = next_marker
            continue
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=RETRY_DELAY_S)
