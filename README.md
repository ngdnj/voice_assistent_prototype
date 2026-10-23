# Голосовой ассистент IT Gorky

Ассистент задач, встреч и напоминаний для российского MAX. Планируемый ввод —
свободный текст и голос; бот и MAX Mini App работают с одними записями пользователя.

Реализована минимальная модель задачи в `app/planner/task.py`: UUID, владелец,
название, статус `todo`/`in_progress`/`done` (начальный `todo`) и явно передаваемое время создания в UTC.
Добавлены прикладной `TaskService`, async-протокол `TaskRepository` и
демонстрационный `InMemoryTaskRepository` и адаптер `RedisTaskRepository`.
Redis временно используется как основное хранилище задач прототипа.
API/worker/dispatcher пока не реализованы. Локальный backend запускается через
`python -m app`; адаптеры LLM/ASR разрабатываются отдельно и здесь не запускаются.

## Быстрый локальный запуск backend

Нужны Python 3.12, `uv` и Docker Compose. Из корня репозитория:

```bash
cp .env.example .env  # только при первом запуске, не заменяйте свою .env
uv sync --locked
docker compose up -d --wait redis
uv run --locked --env-file .env python -m app check
uv run --locked --env-file .env python -m app example --operation-id 31d74683-0692-4a40-8c96-b6add3a3824c
```

`check` проверяет настройки и PING. `example` создаёт фиксированную демонстрационную
задачу и читает её обратно через тот же сервис; выводит `task_id`. Повтор команды
с теми же owner/operation UUID возвращает ту же задачу. Для нового действия
передайте новый UUID. Пример оставляет задачу в Redis; это не отправка в MAX.

Обязательная настройка примера — непустой `BACKEND_DEMO_OWNER_ID` (в шаблоне
`local-demo`). Это доверенная **локальная** идентичность, а не аутентификация
MAX. `check` не требует владельца. Redis имеет локальные значения по умолчанию;
адрес, порт, DB и положительные конечные таймауты проверяются перед запуском.
`BACKEND_SHUTDOWN_TIMEOUT` — положительное конечное число секунд, по умолчанию 5.
Никаких ключей MAX, LLM/ASR для этих команд не требуется. `.env` читает `uv`.

`app/runtime.py` собирает сервисы задач/встреч/напоминаний и Stream-адаптер с
одним Redis-клиентом. Это точка сборки существующего backend, а не имитация
пока отсутствующих API, очереди агента или dispatcher. Команды одноразовые,
завершаются после операции. Демонстратор уведомлений остаётся только в `tests/`:

```bash
uv run --locked --env-file .env python -m tests.notification_consumer
```

Во время операции Ctrl+C (`SIGINT`) или `kill -TERM <PID Python>` прекращает
допуск работы: текущая операция получает до `BACKEND_SHUTDOWN_TIMEOUT` секунд.
Если успевает завершиться — код 0; иначе отменяется, Redis закрывается, код 130.
Повторный сигнал не запускает вторую обработку. Сейчас за запуск выполняется
только одна операция; читателя production-очереди нет. Отмена кооперативная:
операции должны уважать asyncio cancellation, не блокировать event loop и
использовать ограниченные I/O-таймауты. Закрытие Redis идёт после завершения/
отмены операции. Уже сохранённая задача не откатывается при остановке:
повторяйте исходный operation_id, а не создавайте новый.

Ошибочные настройки возвращают код 2 (`invalid_settings`); недоступный Redis —
код 1 (`storage_unavailable`). Ошибки не печатают DSN, пароли или сырые исключения.
JSON-логи содержат события и новый `request_id` на запуск; пример добавляет
`operation_id`. Названия, тексты и owner_id не логируются. Runtime не выполняет
ACK за пользователя: незавершённая доставка остаётся pending. Даже при таком
поведении сбой после внешней отправки до ACK может привести к повторной отправке;
подробнее — [контракт Stream](docs/notification-stream.md).

Проверки на **выделенном тестовом Redis** (локальный Compose не должен содержать
нужные данные; тесты удаляют только собственные ключи):

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked mypy app/runtime.py app/__main__.py
RUN_REDIS_INTEGRATION=1 uv run --locked --env-file .env pytest -q -W error::RuntimeWarning
```

Без `RUN_REDIS_INTEGRATION=1` Redis-тесты пропускаются. Тесты проверяют Stream
(публикацию, чтение, ACK, claim), CLI, SIGTERM/SIGINT во время обработки,
ограничение времени завершения и сохранение pending при отмене.
`.github/workflows/backend.yml` выполняет эти проверки с отдельным Redis service,
без production-секретов и обращений к AI. Использованы официальные инструкции
[uv для GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/) и
[Redis service](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-redis-service-containers).
Остановка локального Redis: `docker compose stop redis`. Текущий Compose не
имеет persistence и тома; сохранность после перезапуска не гарантируется.

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
assert task.status == "todo"
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
    task = await service.create_task(owner_id="user-42", title="Подготовить отчёт", operation_id="example-1")
    assert await service.get_task(task_id=task.id, owner_id="user-42") == task
    assert await service.get_task(task_id=task.id, owner_id="other-user") is None


asyncio.run(main())
```

In-memory хранилище предназначено только для тестов и локальных примеров:
данные привязаны к экземпляру и не сохраняются после перезапуска.
Для смены хранилища реализуйте async-контракт `TaskRepository`:
`add`, `get`, `list`, `get_created` и `update_status`;
сервис и правила модели менять не требуется.

Установка закреплённых зависимостей: `uv sync --locked`.
Проверки: `uv run --locked pytest -q`.
Интеграционный тест Redis включается отдельно, остальные тесты не требуют сервера.

## Локальный Redis

```bash
cp .env.example .env
docker compose up -d --wait redis
uv sync --locked
uv run --locked --env-file .env python -m app.storage.redis
```

Compose запускает Redis 7.4.2 только на `127.0.0.1`, без пароля и без
постоянного тома: это локальный экземпляр для разработки, не production.
Остановка: `docker compose stop redis`.
Если порт занят, измените `REDIS_PORT` в `.env`.

`RedisSettings.from_env()` читает `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`,
`REDIS_USERNAME`, `REDIS_PASSWORD`, `REDIS_CONNECT_TIMEOUT` и
`REDIS_COMMAND_TIMEOUT`. Приложение само не читает `.env`; `uv --env-file`
передаёт его значения в окружение. Оба таймаута по умолчанию — 2 секунды:
подключение и сетевые операции команд. Автоматические повторы команд отключены.
Не логируйте настройки подключения, пароль и исключения с чувствительными данными.

Жизненный цикл процесса (один клиент и его pool на всё время работы,
точка сборки — `app/runtime.py`):

```python
from app.storage.redis import RedisSettings, check_redis, redis_connection


async def run_process():
    async with redis_connection(RedisSettings.from_env()) as redis:
        assert await check_redis(redis)
        # Передать redis нужным инфраструктурным компонентам и работать здесь.
    # aclose() закрывает клиент и его pool, в том числе при ошибке/отмене.
```

Вход в контекст выполняет `PING`; ошибка подключения прерывает запуск и
закрывает ресурсы. Импорт модуля не создаёт клиента. В будущих API/worker
контекст должен охватывать жизнь процесса, а не каждый запрос.
Документация клиента: [redis-py asyncio lifecycle](https://redis.readthedocs.io/en/v8.1.0/examples/asyncio_examples.html).

Интеграционный прогон на **отдельном тестовом Redis** (например, локальном Compose):

```bash
RUN_REDIS_INTEGRATION=1 uv run --locked --env-file .env pytest -q
```

Тест использует уникальный ключ `it-gorky:test:redis:<UUID>:roundtrip`,
проверяет PING/SET/GET и удаляет только свой ключ. TTL 60 секунд ограничивает
время жизни ключа при аварийной остановке; `FLUSHDB` не используется.
Очередь заданий агента пока не реализована. Для уведомлений подготовлен
[контракт Redis Stream для MAX](docs/notification-stream.md): envelope v1,
consumer group, результаты/ACK, восстановление pending и dead-letter.
MAX-клиента и production-потребителя здесь нет; демонстратор находится только
в `tests/notification_consumer.py` и выполняет один шаг без внешней отправки.
Сбой после внешней отправки и до ACK может привести к дублю — Stream не даёт
exactly-once доставку.

## Задачи в Redis: временное хранилище прототипа

`RedisTaskRepository` реализует существующий `TaskRepository`; сервис и его
бизнес-правила не импортируют Redis. Клиент передаётся извне и закрывается
владельцем lifecycle, а не репозиторием.

```python
from datetime import UTC, datetime
from app.planner.service import TaskService
from app.storage.redis import RedisSettings, redis_connection
from app.storage.redis_tasks import RedisTaskRepository


async def task_example():
    async with redis_connection(RedisSettings.from_env()) as client:
        service = TaskService(
            repository=RedisTaskRepository(client),
            clock=lambda: datetime(2026, 10, 21, 9, 30, tzinfo=UTC),
        )
        task = await service.create_task(owner_id="user-42", title="Отчёт", operation_id="example-2")
        assert await service.get_task(task_id=task.id, owner_id="user-42") == task
        assert await service.get_task(task_id=task.id, owner_id="other") is None
```

Схема ключа: `it-gorky:v1:task:<UUID>`. Значение — JSON с полями `id`,
`owner_id`, `title`, `description`, `status`, `created_at`: UUID и статус записываются строками
(старое значение `active` читается как `todo`, запись без `description` — как пустое описание),
время — ISO 8601 с UTC-смещением `+00:00`, включая микросекунды.
`v1` обозначает версию формата. Для изолированных тестов допускается свой
`key_prefix`, например `it-gorky:test:<UUID>`; версия и суффикс задачи сохраняются.
Владельца проверяет репозиторий после чтения записи; чужая задача даёт `None`.

Запись выполняется атомарным Lua-вызовом `SET NX` и обновления индекса, **без TTL**.
Существующий UUID нельзя перезаписать, даже при другом владельце:
выдаётся `TaskAlreadyExistsError`. Прямой add без метаданных защищает только UUID. Сервис требует `operation_id`
и атомарно сохраняет также результат идемпотентности; повтор возвращает исходную задачу.
Повреждённый JSON, неверные поля/UUID/статус/UTC-время и неверный Redis-тип
дают `TaskStorageError`, а не отсутствие записи. Ошибки Redis также преобразуются
в `TaskStorageError` без включения исходных данных в сообщение.

Это временное отклонение от целевой архитектуры с PostgreSQL.
Сохранность при перезапуске Redis зависит от настроек persistence (RDB/AOF)
и тома данных. Текущий Compose отключает persistence и не подключает том:
после перезапуска Redis задачи будут потеряны. Отсутствие TTL не предотвращает
потери при сбоях или вытеснение ключей политикой Redis. Гарантий отсутствия
потерь нет; persistence и том не заменяют резервное копирование.

Интеграционные тесты адаптера запускаются той же командой с
`RUN_REDIS_INTEGRATION=1`. Они проверяют новый экземпляр репозитория и клиента,
все поля, TTL `-1`, отсутствие/чужого владельца, конкурентное добавление,
перезапись и повреждения. Каждый тест удаляет только перечисленные им ключи
в своём уникальном пространстве; пользовательские задачи TTL не получают.

## Встречи

Встречи реализованы отдельно от задач: `Meeting`, `MeetingService`, async-протокол
`MeetingRepository` и `RedisMeetingRepository`. Поля: UUID, владелец, название,
`description`, `starts_at`, `ends_at`, `created_at`. Название очищается по краям; владелец и
название не могут быть пустыми. Все моменты проверяются через общий `to_utc`,
окончание необязательно (`ends_at=None` — длительность неизвестна), а если задано,
обязано быть строго позже начала. Начало и конец задаёт вызывающий код;
длительность не назначается автоматически. Отсутствующее окончание не входит
в отпечаток операции.

```python
from datetime import UTC, datetime
from app.planner.meeting_service import MeetingService
from app.storage.redis_meetings import RedisMeetingRepository


async def meeting_example(client):
    service = MeetingService(
        repository=RedisMeetingRepository(client),
        clock=lambda: datetime(2026, 10, 22, 8, tzinfo=UTC),
    )
    meeting = await service.create_meeting(
        owner_id="user-42",
        title="Обсуждение проекта",
        starts_at=datetime(2026, 10, 22, 9, tzinfo=UTC),
        ends_at=datetime(2026, 10, 22, 9, 45, tzinfo=UTC),
        operation_id="example-3",
    )
    assert await service.get_meeting(meeting_id=meeting.id, owner_id="user-42") == meeting
```

Клиент берётся из существующего `redis_connection`, нового подключения на
операцию нет. Ключ: `it-gorky:v1:meeting:<UUID>`, JSON содержит поля модели (`ends_at` пишется только при наличии окончания; запись без `description` читается с пустым описанием),
UUID и UTC-моменты сериализуются строками без потери микросекунд. Запись —
`SET NX` без TTL; конфликт UUID даёт `MeetingAlreadyExistsError`, ошибки Redis
или повреждённые данные — `MeetingStorageError`. Чужая и отсутствующая встреча
возвращают `None`. Восстановление не генерирует новый UUID или время создания.
Ограничения persistence и резервного копирования выше относятся и к встречам.
Участники, приглашения, интеграции календарей,
обнаружение конфликтов не реализовано.

Проверки правил и сервиса: `tests/test_meeting.py`; интеграционные проверки:
`tests/test_redis_meetings.py`, включаются общим `RUN_REDIS_INTEGRATION=1`.

## Напоминания

Напоминания: отдельные `Reminder`, `ReminderService`, async-протокол
`ReminderRepository` и `RedisReminderRepository`. Обязательны доверенный
`owner_id`, непустой `text` (внешние пробелы удаляются), точный `remind_at`.
Сервис берёт `created_at` из явно переданных часов, UUID генерируется при создании.
Оба момента проверяются существующим `to_utc` и сохраняются в UTC.

**Прошлое время:** если `remind_at < created_at`, создание отклоняется через
`PastReminderTimeError` (подкласс `ValueError`) до записи. При равенстве создание
разрешено. Время не переносится на будущее и не заменяется текущим. Проверка
использует однократное значение часов при создании, а не время завершения записи.
После наступления срока сохранённое напоминание продолжает читаться без изменений.

**Доставка отделена от данных:** модель и JSON содержат только `id`, `owner_id`,
`text`, `description`, `remind_at`, `created_at`. Полей `sent`, статуса доставки и попыток нет.
Отдельная запись состояния доставки в этом блоке не создаётся. Успешное создание
означает только сохранение данных, не постановку на отправку и не отправленное
сообщение. Будущий dispatcher должен хранить состояние отправок отдельно,
ссылаясь на ID напоминания. Получение записи не вычисляет и не подтверждает доставку.

```python
from datetime import UTC, datetime
from app.planner.reminder_service import ReminderService
from app.storage.redis_reminders import RedisReminderRepository


async def reminder_example(client):
    service = ReminderService(
        repository=RedisReminderRepository(client),
        clock=lambda: datetime(2026, 10, 22, 8, tzinfo=UTC),
    )
    reminder = await service.create_reminder(
        owner_id="user-42",
        text="Позвонить Ивану",
        remind_at=datetime(2026, 10, 22, 9, tzinfo=UTC),
        operation_id="example-4",
    )
    assert await service.get_reminder(reminder_id=reminder.id, owner_id="user-42") == reminder
```

Ключ `it-gorky:v1:reminder:<UUID>`, JSON, атомарный `SET NX`, без TTL.
Чужая/отсутствующая запись возвращает `None`, конфликт UUID —
`ReminderAlreadyExistsError`, сбой Redis/повреждение — `ReminderStorageError`.
Ограничения persistence и резервного копирования выше действуют и здесь.
Планировщик, отправка MAX и повторяющиеся напоминания не реализованы. Проверки — `tests/test_reminder.py` и `tests/test_redis_reminders.py`;
интеграционные включаются общим `RUN_REDIS_INTEGRATION=1`.

## Срок задачи

Задача может не иметь срока, иметь `due_date` (дата без времени, хранится как `date`, без
подстановки полуночи) или `due_at` (точный момент, переводится в UTC через `to_utc`). Оба сразу —
`ValueError`. Срок входит в отпечаток операции; отсутствующий срок не добавляется, поэтому
прежние операции повторяются без конфликта. В Redis поля `due_date`/`due_at` пишутся только при
наличии срока; выборка за период по-прежнему идёт по `created_at`.

## Статус задачи

`TaskService.update_status(task_id=..., owner_id=..., status=...)` меняет статус на
`todo`, `in_progress` или `done`; переходы не ограничены, повтор того же статуса ничего
не меняет. Отсутствующая и чужая задача одинаково дают `None`, неизвестный статус —
`ValueError` до обращения к хранилищу. Redis-адаптер перезаписывает запись через
compare-and-set (Lua): если запись изменилась между чтением и записью, попытка
повторяется до 3 раз, затем `TaskStorageError`. Индекс и история операций не меняются;
повтор создания тем же `operation_id` возвращает задачу в текущем состоянии.

## Описание записей

У задачи, встречи и напоминания есть необязательное `description` (по умолчанию `""`).
Крайние пробелы удаляются, длина — до 2000 символов (`app/planner/fields.py`).
Описание входит в отпечаток операции: повтор того же `operation_id` с другим описанием —
`OperationConflictError`. Пустое описание в отпечаток не добавляется, поэтому операции,
сохранённые до появления поля, повторяются без конфликта. Поиск `RecordQuery.query` в Redis
по-прежнему ищет только по названию/тексту; поиск по описанию — PostgreSQL FTS роли 4.

## Идемпотентность создания

Сервисы создания задач, встреч и напоминаний требуют `operation_id` от адаптера.
Область уникальности: владелец + тип операции + operation_id. Те же
нормализованные данные возвращают первоначальные UUID и created_at,
другие — `OperationConflictError`. Запись, индекс и JSON результата сохраняются
одним Lua-вызовом Redis. TTL у истории операций нет: автоматическое истечение
снова разрешило бы создать дубль. Удаление/потеря истории снимает эту защиту;
она не заменяет persistence и backup.
Контракт, формат ключей и **построчный разбор 17 строк Lua**:
[docs/idempotency.md](docs/idempotency.md).

## Структура

Списки и простой поиск доступны через `list_tasks`, `list_meetings`,
`list_reminders` с обязательным `RecordQuery(owner_id=...)`.
Период `[start_time, end_time)` фильтрует `created_at` задачи, `starts_at`
встречи или `remind_at` напоминания. Сортировка — время, затем UUID;
страница — до 100 записей. Поиск `query="текст"` — Unicode casefold-подстрока
с пределом 1000 кандидатов владельца/типа/периода; превышение — явная ошибка.
Для старых данных нужен отдельный backfill индексов перед первой выборкой.
Контракт, пример, схема индексов и команда миграции:
[docs/record-queries.md](docs/record-queries.md).

Минимальные правила времени реализованы в `app/planner/time.py`: aware datetime
в UTC, IANA-зоны, UTC-границы локального дня и интервалы `[start, end)`.
Неоднозначные/несуществующие локальные часы отклоняются; дата остаётся `date`.
Политика DST и примеры — в [backend-контракте](docs/backend-contracts.md#время).

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
Зависимость `redis` закреплена в `pyproject.toml` и `uv.lock`.
pytest, Ruff и mypy пока не настроены. Точки запуска API/worker отсутствуют.
