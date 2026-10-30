"""Domain scope and selective voice confirmation without external providers."""

import pytest

from app.agent.agent import Assistant
from app.agent.history import InMemoryChatHistory
from app.agent.permissions import OFF_TOPIC_REPLY, allowed_mutations, off_topic, voice_requires_confirmation
from tests.agent_support import ScriptedModel
from tests.test_agent import TIME, Gateway


@pytest.mark.parametrize(
    "text",
    [
        "Что такое bogosort на Python?",
        "Сколько травинок на земле?",
        "Объясни квантовую физику",
        "Напиши код игры",
    ],
)
async def test_unrelated_question_does_not_call_model_or_tools(text):
    model, gateway = ScriptedModel(responses=[]), Gateway()
    reply = await Assistant(model, gateway, InMemoryChatHistory()).handle("owner", text, **TIME)
    assert reply.text == OFF_TOPIC_REPLY
    assert not model.seen
    assert not gateway.calls
    assert not voice_requires_confirmation(text)


@pytest.mark.parametrize(
    "text",
    [
        "Какие встречи у меня завтра?",
        "Покажи задачи",
        "Найди напоминание про врача",
        "Во сколько начинается встреча?",
        "Мои планы на завтра",
        "Расписание на сегодня",
        "Встречи завтра",
        "Напоминания на неделю",
        "Вывод встреч",
        "Найди отчёт",
        "Покажи все",
        "А во сколько?",
        "Когда она начинается?",
        "Сколько длится?",
        "Что у меня запланировано?",
        "Что у меня по плану?",
        "Какие у меня дела?",
    ],
)
def test_reads_need_no_voice_confirmation_and_grant_no_write(text):
    assert not off_topic(text)
    assert not voice_requires_confirmation(text)
    assert not allowed_mutations(text)


@pytest.mark.parametrize(
    "text",
    [
        "Добавь задачу купить хлеб",
        "Создай встречу завтра с часу до пяти",
        "Напомни завтра в 10 позвонить врачу",
        "Переименуй задачу в Отчёт",
        "Перенеси встречу на 15:00",
        "Измени напоминание на завтра в 10",
        "Удали задачу",
        "Удали встречу",
        "Удали напоминание",
        "в 15:00",
    ],
)
def test_voice_writes_and_ambiguous_clarifications_require_confirmation(text):
    assert voice_requires_confirmation(text)


def test_task_title_about_programming_is_valid_data():
    text = "Создай задачу «Узнать, что такое bogosort на Python»"
    assert not off_topic(text)
    assert allowed_mutations(text) == {"create_event"}
