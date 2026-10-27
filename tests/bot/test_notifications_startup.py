import asyncio

from app.bot.__main__ import parse_args, stop_background


def test_reminders_are_on_by_default_and_can_be_disabled() -> None:
    assert parse_args([]).reminders
    assert parse_args(["webhook"]).reminders
    assert not parse_args(["webhook", "--no-reminders"]).reminders
    assert not parse_args(["polling", "--no-reminders", "--text-only"]).reminders


async def test_stop_background_stops_loops_and_cancels_stuck_ones(monkeypatch) -> None:
    import app.bot.__main__ as main_module

    monkeypatch.setattr(main_module, "SHUTDOWN_TIMEOUT_S", 0.05)
    stop = asyncio.Event()

    async def polite() -> None:
        await stop.wait()

    async def stuck() -> None:
        await asyncio.Event().wait()

    tasks = [asyncio.create_task(polite()), asyncio.create_task(stuck())]
    await stop_background(tasks, stop)
    assert stop.is_set()
    assert tasks[0].done() and not tasks[0].cancelled()
    assert tasks[1].cancelled()
    await stop_background([], asyncio.Event())  # Nothing started: nothing to do.
