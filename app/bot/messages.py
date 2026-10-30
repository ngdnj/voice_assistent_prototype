"""User-facing texts of the bot. No service names, error codes or traces (root AGENTS §11)."""

from app.asr.base import MAX_DURATION_S

WELCOME = (
    "Здравствуйте! Я помогу с задачами, напоминаниями и встречами.\n"
    "Напишите запрос текстом или запишите голосовое сообщение. Например: "
    "«Напомни завтра в 10 позвонить Ивану» или «Что у меня на пятницу?»"
)
ACCEPTED = "Принял запрос, обрабатываю…"
BUSY = "Сейчас обрабатывается ваш предыдущий запрос. Повторите попытку чуть позже."
INVALID_REQUEST = "Введите корректный запрос."
UNSUPPORTED = "Я понимаю текст и голосовые сообщения. Напишите запрос или запишите голосовое."
NOT_HEARD = "Не расслышал, повторите, пожалуйста."
AUDIO_TOO_LONG = f"Голосовое слишком длинное. Уложитесь, пожалуйста, в {int(MAX_DURATION_S)} секунд."
AUDIO_TOO_LARGE = "Файл записи слишком большой. Запишите, пожалуйста, сообщение покороче."
AUDIO_UNREADABLE = "Не удалось прочитать запись. Попробуйте записать ещё раз или напишите текстом."
AUDIO_FAILED = "Не удалось распознать голосовое. Попробуйте ещё раз чуть позже или напишите текстом."
FAILED = "Не получилось обработать запрос. Попробуйте ещё раз чуть позже."
REPEAT_REQUEST = "Повторите запрос."
# Callback button payloads: short fixed codes, never user data.
CONFIRM_YES = "confirm:yes"
CONFIRM_NO = "confirm:no"
CONFIRM_YES_TEXT = "Да, все верно"
CONFIRM_NO_TEXT = "Нет"
# Voice writes and ambiguous clarifications require transcript confirmation.
VOICE_CONFIRM = "Распознано: {text}\nВсе верно?"
# Payload prefixes; the pending voice id follows them.
VOICE_YES = "voice:yes:"
VOICE_NO = "voice:no:"
VOICE_YES_TEXT = "Да, верно"
VOICE_NO_TEXT = "Нет"
VOICE_EXPIRED = "Этот запрос уже неактуален. Повторите его, пожалуйста."
NOT_ACCEPTED = "Сейчас не получается принять запрос. Попробуйте ещё раз чуть позже."
START_COMMANDS = frozenset({"/start", "/help"})
APP_COMMANDS = frozenset({"/app"})
OPEN_APP_TEXT = "Открыть приложение"
APP_INVITE = "Задачи, напоминания и встречи можно посмотреть в приложении."
APP_UNAVAILABLE = "Приложение пока недоступно. Пользуйтесь запросами текстом или голосом."

# Text of a due reminder; the name and description come from the saved record.
REMINDER = "🔔 Напоминание: {name}"
