"""Relative dates come from the request's reference_time in the user's zone (AGENTS §7)."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastmcp import Client
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatResult
from pydantic import Field

from assistant.llm.agent import Assistant, Pricing
from assistant.llm.history import InMemoryChatHistory
from assistant.llm.prompts import render_system_prompt, user_timezone
from tests.conftest import TZ, ToolCallingFakeModel

YEKB = ZoneInfo("Asia/Yekaterinburg")
BERLIN = ZoneInfo("Europe/Berlin")


def test_day_boundary_one_second_before_midnight() -> None:
    prompt = render_system_prompt(datetime(2026, 10, 19, 23, 59, 59, tzinfo=TZ), TZ)
    assert "Сегодня: понедельник" in prompt
    assert "2026-10-19 — понедельник, эта неделя (сегодня)" in prompt
    assert "2026-10-20 — вторник, эта неделя (завтра)" in prompt
    assert "- через 15 минут: 2026-10-20T00:14:00+03:00 (завтра)" in prompt


def test_week_boundary_sunday_night_and_monday_midnight() -> None:
    sunday = render_system_prompt(datetime(2026, 10, 25, 23, 30, tzinfo=TZ), TZ)
    assert "2026-10-25 — воскресенье, эта неделя (сегодня)" in sunday
    assert "2026-10-26 — понедельник, следующая неделя (завтра)" in sunday

    monday = render_system_prompt(datetime(2026, 10, 26, 0, 0, tzinfo=TZ), TZ)
    assert "2026-10-26 — понедельник, эта неделя (сегодня)" in monday
    assert "2026-11-02 — понедельник, следующая неделя" in monday


def test_same_instant_in_another_zone_is_another_day() -> None:
    instant = datetime.fromisoformat("2026-10-19T20:30:00+00:00")

    moscow = render_system_prompt(instant, TZ)
    assert "Время запроса: 2026-10-19T23:30:00+03:00" in moscow
    assert "Сегодня: понедельник" in moscow

    yekb = render_system_prompt(instant, YEKB)
    assert "Часовой пояс пользователя: Asia/Yekaterinburg" in yekb
    assert "Время запроса: 2026-10-20T01:30:00+05:00" in yekb
    assert "Сегодня: вторник" in yekb
    assert "2026-10-21 — среда, эта неделя (завтра)" in yekb
    assert "- через 2 часа: 2026-10-20T03:30:00+05:00 (сегодня)" in yekb


def test_offsets_are_real_hours_across_dst_change() -> None:
    # Berlin leaves summer time on 2026-10-25 at 03:00 CEST -> 02:00 CET.
    prompt = render_system_prompt(datetime(2026, 10, 25, 1, 30, tzinfo=BERLIN), BERLIN)
    assert "Время запроса: 2026-10-25T01:30:00+02:00" in prompt
    # 23:30Z + 2 h = 01:30Z = 02:30 CET, not 03:30 as wall-clock arithmetic gives.
    assert "- через 2 часа: 2026-10-25T02:30:00+01:00 (сегодня)" in prompt


def test_rejects_naive_time_and_unknown_zone() -> None:
    with pytest.raises(ValueError):
        render_system_prompt(datetime(2026, 10, 19, 12, 0), TZ)
    with pytest.raises(ValueError):
        user_timezone("Moscow/Kremlin")
    with pytest.raises(ValueError):
        user_timezone("")
    assert user_timezone("Asia/Yekaterinburg") == YEKB


class RecordingModel(ToolCallingFakeModel):
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def _system_prompt(messages: list[BaseMessage]) -> str:
    [system] = [m for m in messages if isinstance(m, SystemMessage)]
    return system.text


async def test_retry_after_midnight_keeps_reference_time(mcp_client: Client) -> None:
    """A request received at 23:59:50 and retried later still means Monday's "завтра"."""
    model = RecordingModel(responses=[AIMessage("Во сколько?"), AIMessage("Во сколько?")])
    assistant = Assistant(model, mcp_client, InMemoryChatHistory(10), Pricing(0, 0))
    received = datetime(2026, 10, 19, 23, 59, 50, tzinfo=TZ)

    for _ in range(2):  # the second call stands for a retry by the job runner
        await assistant.handle("u1", "Встреча завтра", reference_time=received, timezone="Europe/Moscow")

    first, retry = (_system_prompt(m) for m in model.seen)
    assert first == retry
    assert "Время запроса: 2026-10-19T23:59:50+03:00" in first
    assert "2026-10-20 — вторник, эта неделя (завтра)" in first


async def test_agent_uses_user_zone_not_server_zone(mcp_client: Client) -> None:
    model = RecordingModel(responses=[AIMessage("Привет")])
    assistant = Assistant(model, mcp_client, InMemoryChatHistory(10), Pricing(0, 0))

    utc_time = datetime.fromisoformat("2026-10-19T20:30:00+00:00")
    await assistant.handle("u1", "Привет", reference_time=utc_time, timezone="Asia/Yekaterinburg")

    prompt = _system_prompt(model.seen[0])
    assert "Время запроса: 2026-10-20T01:30:00+05:00" in prompt
    assert "Сегодня: вторник" in prompt
