from datetime import timedelta

import pytest

from assistant.events.models import EventType
from assistant.events.storage import EventStorage
from tests.conftest import NOW


def test_list_between_filters_by_user_type_and_range(storage: EventStorage) -> None:
    storage.create("u1", EventType.MEETING, "Встреча с командой", "", NOW + timedelta(hours=3))
    storage.create("u1", EventType.TASK, "Отчёт", "", NOW + timedelta(days=4))
    storage.create("u2", EventType.MEETING, "Чужая встреча", "", NOW + timedelta(hours=3))

    day_start = NOW.replace(hour=0)
    day_end = NOW.replace(hour=23, minute=59, second=59)

    today = storage.list_between("u1", [], day_start, day_end)
    assert [e.name for e in today] == ["Встреча с командой"]
    assert storage.list_between("u1", [EventType.TASK], day_start, day_end) == []


def test_datetimes_round_trip_in_local_timezone(storage: EventStorage) -> None:
    event = storage.create("u1", EventType.REMINDER, "Позвонить", "", NOW)
    assert event.when == NOW
    assert event.when is not None and event.when.utcoffset() == timedelta(hours=3)


def test_naive_datetime_is_rejected(storage: EventStorage) -> None:
    with pytest.raises(ValueError):
        storage.create("u1", EventType.TASK, "x", "", NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("презентация", "Подготовить презентацию"),
        ("найди задачу про презентацию", "Подготовить презентацию"),
        ("встреча с Иваном", "Созвон с Иваном"),
        ("договору", "Проверить договор"),
    ],
)
def test_search_matches_inflected_forms(storage: EventStorage, query: str, expected: str) -> None:
    storage.create("u1", EventType.TASK, "Подготовить презентацию", "для клиента", None)
    storage.create("u1", EventType.MEETING, "Созвон с Иваном", "обсудить бюджет", NOW)
    storage.create("u1", EventType.TASK, "Проверить договор", "аренда офиса", None)

    results = storage.search("u1", query)
    assert results and results[0].name == expected


def test_search_is_isolated_per_user(storage: EventStorage) -> None:
    storage.create("u2", EventType.TASK, "Проверить договор", "", None)
    assert storage.search("u1", "договор") == []


def test_find_same_matches_type_name_and_time(storage: EventStorage) -> None:
    storage.create("u1", EventType.TASK, "Презентация", "", NOW)
    assert storage.find_same("u1", EventType.TASK, "презентация ", NOW) is not None
    assert storage.find_same("u1", EventType.TASK, "Презентация", NOW + timedelta(days=1)) is None
    assert storage.find_same("u1", EventType.MEETING, "Презентация", NOW) is None
    assert storage.find_same("u2", EventType.TASK, "Презентация", NOW) is None


def test_find_same_without_time(storage: EventStorage) -> None:
    storage.create("u1", EventType.TASK, "Разобрать почту", "", None)
    assert storage.find_same("u1", EventType.TASK, "Разобрать почту", None) is not None


def test_delete_only_own_record(storage: EventStorage) -> None:
    event = storage.create("u1", EventType.MEETING, "Встреча с Иваном", "", NOW)
    assert storage.delete("u2", event.id) is None
    assert storage.search("u1", "Иван")
    deleted = storage.delete("u1", event.id)
    assert deleted is not None and deleted.id == event.id
    assert storage.search("u1", "Иван") == []
    assert storage.delete("u1", event.id) is None
