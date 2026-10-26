from app.llm.speech import to_chat_text, to_speech_text
from evals.checks import speech_issues


def test_plain_text_is_unchanged() -> None:
    assert to_speech_text("Встреча с командой завтра в 15:00.") == "Встреча с командой завтра в 15:00."


def test_chat_keeps_numbering_and_line_breaks_while_speech_removes_them():
    raw = "Встречи:\n1) **Хакатон** в 10:00\n2) Созвон в 15:00"
    chat = to_chat_text(raw)
    assert chat == "Встречи:\n1) Хакатон в 10:00\n2) Созвон в 15:00"
    assert "\n" not in to_speech_text(chat) and "1)" not in to_speech_text(chat)


def test_markdown_list_becomes_one_spoken_line() -> None:
    raw = (
        "На эту неделю запланировано четыре дела:\n\n"
        "— **Созвон с командой** (встреча) — сегодня в 16:00\n"
        "- Оплатить интернет — сегодня в 20:00\n"
        "1. Купить подарок маме — завтра 👍"
    )
    spoken = to_speech_text(raw)
    assert speech_issues(spoken) == []
    assert spoken == (
        "На эту неделю запланировано четыре дела: Созвон с командой (встреча) — сегодня в 16:00; "
        "Оплатить интернет — сегодня в 20:00; Купить подарок маме — завтра"
    )


def test_dash_inside_sentence_is_kept() -> None:
    assert to_speech_text("Сегодня в 16:00 — созвон.") == "Сегодня в 16:00 — созвон."
