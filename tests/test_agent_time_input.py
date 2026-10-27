import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.time_input import has_explicit_time


@pytest.mark.parametrize(
    "text",
    [
        "Напомни завтра сходить в поликлинику",
        "Встреча в пятницу",
        "завтра вечером",
        "утром",
        "2026-10-27",
    ],
)
def test_day_or_part_of_day_does_not_supply_an_hour(text):
    assert not has_explicit_time(text)


@pytest.mark.parametrize(
    "text",
    [
        "завтра в 9",
        "завтра в 09:30",
        "в девять",
        "к двум",
        "через два часа",
        "через 15 минут",
        "через час",
        "через полчаса",
        "через сутки",
        "2026-10-27T16:00:00+03:00",
        "16",
        "в полдень",
        "завтра встреча с часу до пяти по обсуждению хакатона",
        "с часу",
        "с одного до пяти",
        "с двух до четырёх",
        "к тринадцати",
        "в двадцать три часа",
        "пять вечера",
        "в половине третьего дня",
        "полтретьего дня",
        "без четверти шесть вечера",
        "в четверть шестого вечера",
        "без десяти восемь",
        "Завтра встреча по обсуждению хакатона, 10 утра",
        "Напомни завтра 10 утра позвонить врачу",
        "Завтра созвон 5 вечера",
        "Встреча завтра 10 часов утра",
        "Завтра встреча 10утра",
    ],
)
def test_explicit_clock_or_duration_supplies_time(text):
    assert has_explicit_time(text)


def test_time_can_be_inherited_only_from_a_clarification():
    original = HumanMessage("Завтра в 16 встреча")
    assert has_explicit_time("С Иваном", [original, AIMessage("С кем встреча?")])
    assert not has_explicit_time("Завтра поликлиника", [original, AIMessage("Встреча создана.")])
    assert has_explicit_time("Да", [HumanMessage("Напомни завтра"), AIMessage("Напомнить в 9 утра?")])


@pytest.mark.parametrize("text", ["Завтра 25 утра встреча", "Завтра 10 задач", "Встреча 27 октября утром"])
def test_numbers_without_a_valid_hour_and_day_part_are_not_time(text):
    assert not has_explicit_time(text)
