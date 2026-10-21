# Голосовой ассистент IT Gorky

Ассистент задач, встреч и напоминаний для российского MAX. Планируемый ввод —
свободный текст и голос; бот и MAX Mini App работают с одними записями пользователя.

Реализована минимальная модель задачи в `app/planner/task.py`: UUID, владелец,
название, начальный статус `active` и явно передаваемое время создания в UTC.
Добавлены прикладной `TaskService`, async-протокол `TaskRepository` и
демонстрационный `InMemoryTaskRepository`. Постоянного хранения, работающих
интеграций и точек запуска сервисов пока нет.

Пример создания в памяти (Python 3.12+):

```python
from datetime import UTC, datetime
from app.planner.task import create_task

task = create_task(
    owner_id="user-42",
    title="  Подготовить отчёт  ",
    created_at=datetime(2026, 10, 21, 9, 30, tzinfo=UTC),
)
assert task.title == "Подготовить отчёт"
assert task.status == "active"
```

Создание и получение через прикладной сервис:

```python
import asyncio
from datetime import UTC, datetime

from app.planner.service import TaskService
from app.storage.memory import InMemoryTaskRepository

async def main():
    service = TaskService(
        repository=InMemoryTaskRepository(),
        clock=lambda: datetime(2026, 10, 21, 9, 30, tzinfo=UTC),
    )
    task = await service.create_task(owner_id="user-42", title="Подготовить отчёт")
    assert await service.get_task(task_id=task.id, owner_id="user-42") == task
    assert await service.get_task(task_id=task.id, owner_id="other-user") is None

asyncio.run(main())
```

In-memory хранилище предназначено только для тестов и локальных примеров:
данные привязаны к экземпляру и не сохраняются после перезапуска.
Для смены хранилища передайте реализацию двух async-методов `add` и `get`;
сервис и правила модели менять не требуется.

Проверки без внешних зависимостей: `python3 -m unittest discover -s tests -v`.

## Структура

```text
app/
├── __init__.py
├── bot/          # MAX: входящие события, аудиовложения и отправка ответов
├── api/          # HTTP-маршруты, webhook, REST для Mini App, подключение MCP
├── agent/        # Диалог, история, ASR/LLM и цикл вызовов инструментов
├── asr/          # Распознавание речи и адаптеры ASR-провайдеров
├── llm/          # Клиенты моделей, промпт и преобразование запросов/ответов
├── tts/          # Необязательный синтез готового ответа
├── planner/      # Сущности и бизнес-операции задач, встреч и напоминаний
├── mcp_server/   # Типизированные MCP-инструменты поверх planner
├── storage/      # PostgreSQL и Redis: доступ и хранение
└── workers/      # Фоновые задания, dispatcher, ответы и напоминания
tests/           # Проверки правил, компонентов и интеграций
evals/           # Оценка качества ASR и понимания запросов
docs/            # Архитектурные решения и документация
```

Каждый каталог внутри `app/` содержит пустой `__init__.py`.
В `tests/test_task.py` находятся проверки модели, в `tests/test_task_service.py` —
сервиса и демонстрационного хранилища; `evals/` пока содержит `.gitkeep`.
Внутренние подпапки и файлы реализации появятся вместе с конкретными сценариями.

## Обязательные сценарии

1. Создать задачу или напоминание.
2. Создать встречу.
3. Получить задачи и встречи за указанный период.
4. Найти ранее созданные записи по естественному запросу.

Данные должны сохраняться между запросами и перезапусками. При нехватке обязательных
полей ассистент задаёт уточняющий вопрос. Текстовый ответ на голос допустим;
TTS и доставка напоминаний по расписанию добавляются после основного пути.
Mini App предусматривает период, вкладки задач/напоминаний/встреч и смену статуса задачи.

## Архитектура и дальнейшая разработка

Один Python-проект с корневым пакетом `app/` заменяет прежние пути
`backend/src/assistant/` и `backend/tests/`; проверки размещаются в `tests/`.
Остальные согласованные требования сохраняются, правила работы — в [AGENTS.md](AGENTS.md).
Ответственность модулей и путь запроса описаны в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Контекст системы, процессы и компоненты API показаны в [C4-представлениях](docs/C4.md).

Целевой стек: Python 3.12, FastAPI/Pydantic, LangChain/GigaChat, FastMCP,
PostgreSQL/SQLAlchemy/asyncpg/Alembic, Redis Streams. Поиск MVP — PostgreSQL FTS
с русской конфигурацией и GIN. Один основной ASR выбирается после проверки
GigaAM и Yandex SpeechKit. Mini App — React/TypeScript/Vite/MAX Bridge;
развёртывание — Docker Compose и HTTPS reverse proxy; наблюдаемость —
структурированные логи, Prometheus и Grafana.

Создание задачи в памяти и проверки её правил реализованы.
При появлении зависимостей добавим `pyproject.toml` и `uv.lock`, затем команды
запуска и проверки через pytest, Ruff и mypy. Сейчас установка и запуск не предусмотрены.
