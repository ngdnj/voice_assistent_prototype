import asyncio

import pytest

from app.bot import messages, polling
from app.bot.client import PollingError
from tests.bot.fakes import FakeGate, build_bot

START = {"update_type": "bot_started", "timestamp": 1, "chat_id": 777, "user": {"user_id": 42}}


class FakePollingClient:
    def __init__(self, gate: FakeGate, stop: asyncio.Event) -> None:
        self.gate = gate
        self.stop = stop
        self.markers: list[int | None] = []

    async def get_updates(
        self, *, marker: int | None, timeout_s: int, types: tuple[str, ...]
    ) -> tuple[list[object], int | None]:
        self.markers.append(marker)
        call = len(self.markers)
        if call == 1:
            raise PollingError("network", code="connect_error")
        if call == 2:
            self.gate.fail = True  # Storage fails: the batch must be fetched again.
            return [START, {"bad": "update"}], 5
        if call == 3:
            self.gate.fail = False
            return [START, {"bad": "update"}], 5
        self.stop.set()
        return [], 6


async def test_batch_is_confirmed_only_after_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(polling, "RETRY_DELAY_S", 0.0)
    gate = FakeGate()
    bot, parts = build_bot(gate=gate)
    stop = asyncio.Event()
    client = FakePollingClient(gate, stop)
    await polling.run_polling(client, bot, stop)  # type: ignore[arg-type]
    assert client.markers == [None, None, None, 5]
    assert parts["sender"].texts == [messages.WELCOME]  # type: ignore[attr-defined]
