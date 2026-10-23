# План реализации роли 4: PostgreSQL, MCP, REST для Mini App

Живой документ. Перед каждым блоком агент перечитывает его. Новые решения, уточнения и
изменения архитектуры вносятся сюда **до** кода. Статусы: ☐ не начато, ◐ в работе, ✅ готово (с датой).

Правила работы: [`AGENTS.md`](AGENTS.md). Контракт для других ролей: [`INTEGRATION.md`](INTEGRATION.md).

## Состояние на 2026-10-23

- `app/mcp_server/` содержит только `__init__.py` и эту документацию.
- Хранилище — временный Redis (`app/storage/redis_{tasks,meetings,reminders}.py`, идемпотентность через Lua).
- В проекте нет PostgreSQL, SQLAlchemy, asyncpg, Alembic и FastAPI. FastMCP 4.1.3 (`mcp` 2.3.0), pydantic 2.14.1,
  starlette 1.9.0 и uvicorn 0.54.0 уже закреплены в `uv.lock` (добавлены ролью 3).
- Роль 3 ждёт MCP по `mcp_url` (`app/config.py`), её агент описан в `docs/ML.md`. Файлы `app/agent/tools.py` и
  `tests/fakes/mcp_server.py`, упомянутые там, отсутствуют в `main` и в `origin/feature/ml-llm-asr`.
  Сверять схемы с ними пока не с чем: ориентир — текст `docs/ML.md` и `docs/ML_HANDOFF.md` (T2).

## Принятые решения

| № | Решение | Источник / дата |
| --- | --- | --- |
| D1 | Изменения `app/planner/` делает роль 4, каждое отдельно подтверждает пользователь | пользователь, 2026-10-23 |
| D2 | У встречи необязательный `end_time`; без него `ends_at = NULL`, длительность не выдумывается | пользователь, 2026-10-23 |
| D3 | Субагент тестирования — `.claude/agents/mcp-tester.md`, только локально (`.claude/` в .gitignore) | пользователь, 2026-10-23 |
| D4 | Точечные добавления в `pyproject.toml`/`uv.lock`, `compose.yaml`, `.env.example`, CI, `README.md` разрешены | пользователь, 2026-10-23 |
| D5 | Одна таблица `events` для трёх типов вместо трёх таблиц: общий период и FTS-поиск одним запросом, `CHECK` по типу | роль 4, 2026-10-23 |
| D6 | Идемпотентность — отдельная таблица `operations` с PK `(owner_id, operation_type, operation_id)`, вставка в одной транзакции с записью | AGENTS §9, `docs/idempotency.md` |
| D7 | `delete_event` не входит в MVP; инструментов три | AGENTS §8, ML_HANDOFF T2 |
| D8 | Внешнее `name` соответствует `Task.title` и `Reminder.text`. Маппинг делается в адаптере, поля planner не переименовываются | AGENTS §7 (последний абзац) |
| D9 | Ошибки инструментов возвращаются в результате (`{"error": {...}}`), а не исключением MCP: агенту роли 3 нужен JSON (`ToolCall.ok`) | `docs/ML.md` |

## Блоки

### B0. Изменения planner ✅ 2026-10-23 (unit и Redis-интеграция) (каждый пункт требует разрешения пользователя)

| Пункт | Что | Статус |
| --- | --- | --- |
| B0.1 | `Task.status`: `todo`, `in_progress`, `done`, начальный `todo` (было `Literal["active"]`) | ✅ 2026-10-23 |
| B0.2 | `description: str` (по умолчанию `""`, ≤ 2000, крайние пробелы удаляются) у `Task`, `Meeting`, `Reminder` | ✅ 2026-10-23 |
| B0.3 | Срок задачи: `due_date: date \| None` XOR `due_at: datetime \| None`; без срока допустимо | ✅ 2026-10-23 |
| B0.4 | `Meeting.ends_at: datetime \| None`; при наличии должно быть `> starts_at` (D2) | ✅ 2026-10-23 |
| B0.5 | `TaskService.update_status(task_id, owner_id, status)` и `TaskRepository.update_status` | ✅ 2026-10-23 |
| B0.6 | Отпечаток `CreationOperation` включает новые поля | ✅ 2026-10-23: `description`, срок, `ends_at`; отсутствующие значения не входят в отпечаток |

B0.1, детали: `TaskStatus`/`TASK_STATUSES` в `app/planner/task.py`; `status` теперь передаётся в конструктор
(нужно для восстановления из хранилища), `create_task` по-прежнему всегда создаёт `todo`. Смена статуса — B0.5.
С разрешения B0.1 поправлен `app/storage/redis_tasks.py` (роль 1): читает три статуса, старое `active` → `todo`.

B0.2, детали: правило в `app/planner/fields.py` (`normalize_description`, `description_fingerprint_data`).
Пустое описание не добавляется в отпечаток — иначе сохранённые до B0.2 операции дали бы `operation_conflict`
при повторе. Redis-адаптеры трёх типов (роль 1) пишут `description` и читают записи без него как `""`.
Поиск в Redis по описанию не расширялся (поиск по описанию — FTS в B3).

B0.3, детали: `normalize_due` в `app/planner/task.py` — общее правило для модели и сервиса (проверка до
обращения к хранилищу). Отсутствующий срок не входит в отпечаток. Redis-адаптер задач пишет `due_date`/`due_at`
только при наличии; выборка задач за период в Redis по-прежнему по `created_at` (перевод на срок — B3/Q2).

B0.4, детали: `ends_at` по умолчанию `None`, в `create_meeting`/`MeetingService.create_meeting` стал
необязательным keyword-аргументом (все вызовы именованные). Старые операции всегда содержали `ends_at`,
поэтому их отпечаток не меняется. Redis-адаптер встреч пишет `ends_at` только при наличии.

B0.5, детали: переходы не ограничены, повтор — no-op, чужая/отсутствующая → `None`. Redis: compare-and-set
(`REPLACE_IF_UNCHANGED`, до 3 попыток). Повтор создания после смены статуса возвращает текущую задачу:
проверка результата операции в `redis_tasks._operation_result` теперь сравнивает запись без учёта статуса,
а не побайтно (раньше смена статуса сделала бы повтор `create_task` ошибкой хранилища). Для PostgreSQL (B3)
то же правило: результат операции ссылается на `event_id`, возвращается текущее состояние записи.

Риск: Redis-репозитории роли 1 сериализуют старый набор полей. Их обновление согласовывается отдельно,
до этого Redis работает только со старым форматом. Варианты на выбор роли 1: обновить Redis-адаптеры
или перейти на PostgreSQL как основное хранилище в B6.

### B1. Инфраструктура PostgreSQL ✅ 2026-10-23 (интеграция на Compose PostgreSQL 18.6)

- Зависимости: `sqlalchemy[asyncio]>=2.1,<2.2` (2.1.6), `asyncpg>=0.32,<0.33` (0.32.0), `alembic>=1.20,<2` (1.20.1).
- `compose.yaml`: сервис `postgres` — `postgres:18-alpine` (версия совпадает с локально установленным у пользователя
  PostgreSQL 18), `127.0.0.1:5433` (5432 может быть занят локальным сервером), том `postgres-data` на
  `/var/lib/postgresql` (раскладка образов 18), healthcheck `pg_isready`, `docker/postgres/init-test-db.sql` создаёт
  `it_gorky_test` при пустом томе.
- Ruff: `app/storage` в `extend-exclude` заменён явным списком файлов роли 1, чтобы `app/storage/postgres` проверялся.
  mypy: `app/storage/postgres` добавлен в `files`.
- CI пока не трогал: PostgreSQL-сервис в `.github/workflows/backend.yml` добавлю, когда интеграционные тесты пройдут
  локально (иначе первый прогон с непроверенными тестами сломает CI всем ролям).
- `.env.example`: `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` с
  безопасными примерами и отдельный `POSTGRES_TEST_DB`.
- `app/storage/postgres/settings.py` (`PostgresSettings.from_env()` в стиле `RedisSettings`) и `engine.py`
  (`postgres_engine(settings)` — async context manager, `async_sessionmaker`, без подключения при импорте).
- Тесты: настройки и проверка подключения. Флаг `RUN_PG_INTEGRATION=1`.
- Готово, когда `docker compose up -d --wait postgres` и `RUN_PG_INTEGRATION=1 pytest tests/storage_pg` проходят.

### B2. Схема и миграции ☐

Таблица `events`:

| Колонка | Тип | Ограничения |
| --- | --- | --- |
| `id` | `UUID` PK | |
| `owner_id` | `TEXT` | NOT NULL, не пустая |
| `event_type` | `TEXT` | `IN ('task','meeting','reminder')` |
| `name` | `TEXT` | `btrim(name) <> ''`, длина ≤ 200 |
| `description` | `TEXT` | NOT NULL DEFAULT `''`, длина ≤ 2000 |
| `status` | `TEXT NULL` | у `task` — `IN ('todo','in_progress','done')`, у остальных `NULL` |
| `due_date` | `DATE NULL` | только у `task` |
| `starts_at` | `TIMESTAMPTZ NULL` | `NOT NULL` у meeting/reminder; у task — срок `due_at`; XOR с `due_date` |
| `ends_at` | `TIMESTAMPTZ NULL` | только у meeting, `> starts_at` |
| `created_at` | `TIMESTAMPTZ` | NOT NULL |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL |
| `search` | `tsvector` GENERATED STORED | `to_tsvector('russian', name \|\| ' ' \|\| description)` |

Таблица `operations`: `owner_id`, `operation_type`, `operation_id` (PK), `fingerprint CHAR(64)`,
`event_id UUID FK events`, `created_at`.

Индексы: `(owner_id, starts_at)`, `(owner_id, due_date)`, `(owner_id, event_type, created_at)`, `GIN(search)`.

- Alembic в `migrations/`, `alembic.ini` в корне, async env. Первая миграция проверяется вручную.
- Тесты: upgrade/downgrade на пустой тестовой БД, CHECK-ограничения отклоняют неверные строки.

### B3. Репозитории PostgreSQL ☐

- `app/storage/postgres/{tasks,meetings,reminders}.py` реализуют протоколы planner: `add`, `get`, `list`, `get_created`,
  `update_status` (только задачи).
- `add(record, operation=...)` — одна транзакция: `INSERT events` + `INSERT operations ON CONFLICT DO NOTHING`.
  При конфликте выполняется откат и чтение прежнего результата: совпал отпечаток — вернуть прежнюю запись,
  не совпал — `OperationConflictError`.
- `list(RecordQuery)`: фильтр владельца, полуинтервал, порядок `(time, id)`, `limit`/`offset` по правилам
  `app/planner/query.py`. Поиск: `search @@ websearch_to_tsquery('russian', :q)`, сортировка `ts_rank`, затем время.
- Ошибки драйвера → `*StorageError` (существующие классы planner) без исходного текста.
- Тесты на реальной тестовой БД: сохранность после нового engine, чужой владелец, параллельные повторы
  (`asyncio.gather` с отдельными сессиями), конфликт, границы периода и DST, русская морфология
  («встреча» находит «встречи»), пустая выдача.

### B4. MCP server ☐

- Перед кодом сверить API FastMCP 4.1.3 (https://gofastmcp.com/): объявление инструментов, доступ к HTTP-заголовкам
  из контекста, streamable HTTP, монтирование в ASGI.
- `app/mcp_server/schemas.py`: Pydantic-модели аргументов, `EventDTO`, `ToolError`.
- `app/mcp_server/context.py`: извлечение и проверка доверенного контекста (сервисный токен через
  `hmac.compare_digest`, `owner_id`, `action_id`, `request_id`).
- `app/mcp_server/server.py`: `build_mcp_server(services) -> FastMCP`, три инструмента, сборка без сети при импорте.
- Тесты: in-memory клиент FastMCP. Проверяются схемы инструментов (нет `user_id` в аргументах), валидация,
  отсутствие или неверный токен, повтор `action_id`, пустая выдача, ошибка хранилища → `temporary_unavailable`
  без ложного `created`.

### B5. REST для Mini App ☐

- Добавить `fastapi` (если роль 1 не добавит раньше).
- `app/api/init_data.py`: проверка подписи `initData` строго по документации MAX, свежесть `auth_date`
  (окно конфигурируется), результат — доверенный `owner_id`.
- `app/api/records.py` (`APIRouter`):
  - `GET /api/events?start_time=&end_time=&event_types=` — период;
  - `GET /api/events/search?query=&event_types=&start_time=&end_time=&limit=` — поиск;
  - `PATCH /api/tasks/{task_id}/status` с телом `{"status": "done"}`.
- Ответы используют те же DTO, что и MCP.
- Тесты: подделанная, просроченная и отсутствующая initData, чужая задача → 404, неверный статус → 422,
  пустой период → `[]`.

### B6. Интеграция ☐

- Предложить роли 1 подключение: роутер и MCP ASGI-приложение в общий FastAPI (`app/api`), PostgreSQL в `app/runtime.py`.
- CI: сервис `postgres` и `RUN_PG_INTEGRATION=1` в `.github/workflows/backend.yml`.
- README: раздел «PostgreSQL, MCP и REST» с запуском.
- `INTEGRATION.md` → v1 (реализовано), сообщить ролям 1, 3, 5 о расхождениях с ML-заглушкой.

## Открытые вопросы

| № | Вопрос | Кому | Статус |
| --- | --- | --- | --- |
| Q1 | Где монтируются MCP и REST: общий FastAPI в `app/api/main.py` (роль 1) или отдельный процесс роли 4 на время разработки | роль 1 | открыт |
| Q2 | Попадают ли задачи без срока в `get_events_by_period`. Предложение: нет (фильтр по сроку), для «все задачи» — отдельный флаг `include_undated` | роли 3, 5 | открыт |
| Q3 | Нужен ли перенос данных из Redis в PostgreSQL или прототипные данные можно не переносить | роль 1 | открыт |
| Q4 | Задача с `due_at` (точное время): разрешаем или только `date`? AGENTS §8 допускает `datetime` для срока | роль 3 | ✅ разрешаем (пользователь, 2026-10-23, B0.3) |
| Q5 | Точное имя заголовка и формат передачи `initData` из Mini App | роль 5 + документация MAX | открыт |
| Q6 | Окно свежести `auth_date` (предложение: 24 ч) | роль 5 | открыт |
| Q7 | Ответ `create_event` при повторе: тот же `{"created": …}` или пометка `"replayed": true` | роль 3 | предложение: тот же + `replayed` |

## Журнал изменений архитектуры и общих файлов

| Дата | Изменение | Файлы |
| --- | --- | --- |
| 2026-10-23 | Созданы правила роли 4, план и черновик контракта v0; локальный субагент `mcp-tester` | `app/mcp_server/{AGENTS,mcp_realisation_plan,INTEGRATION}.md`, `.claude/agents/mcp-tester.md` |
| 2026-10-23 | B0.1: статусы задачи `todo/in_progress/done`; Redis-адаптер задач читает их и legacy `active`; README и `docs/backend-contracts.md` обновлены | `app/planner/task.py`, `app/storage/redis_tasks.py`, `tests/test_task*.py`, `tests/test_redis_tasks.py`, `README.md`, `docs/backend-contracts.md` |
| 2026-10-23 | B0.2: `description` у трёх типов, отпечаток операций совместим со старыми; Redis-адаптеры читают записи без поля | `app/planner/{fields,task,meeting,reminder,service,meeting_service,reminder_service}.py`, `app/storage/redis_{tasks,meetings,reminders}.py`, `tests/test_description.py`, `tests/test_redis_*.py`, `tests/test_reminder.py`, `README.md` |
| 2026-10-23 | B0.3: срок задачи `due_date` XOR `due_at`; Q4 закрыт; Redis-адаптер задач хранит срок | `app/planner/{task,service}.py`, `app/storage/redis_tasks.py`, `tests/test_task_due.py`, `tests/test_redis_tasks.py`, `README.md`, `docs/{backend-contracts,record-queries}.md` |
| 2026-10-23 | B0.4: необязательное окончание встречи; B0.6 закрыт кодом | `app/planner/{meeting,meeting_service}.py`, `app/storage/redis_meetings.py`, `tests/test_meeting_end.py`, `tests/test_redis_meetings.py`, `README.md` |
| 2026-10-23 | B0.5: смена статуса задачи; повтор создания возвращает текущее состояние | `app/planner/{repository,service}.py`, `app/storage/{memory,redis_tasks}.py`, `tests/test_task_status.py`, `tests/test_redis_tasks.py`, `README.md` |
| 2026-10-23 | B0 проверен unit-тестами; B1: зависимости PostgreSQL, Compose, настройки и engine, тесты | `pyproject.toml`, `uv.lock`, `compose.yaml`, `.env.example`, `docker/postgres/init-test-db.sql`, `app/storage/postgres/{settings,engine}.py`, `tests/storage_pg/`, `README.md` |
| 2026-10-23 | Полный прогон с `RUN_REDIS_INTEGRATION=1 RUN_PG_INTEGRATION=1`: 179 passed, 2 skipped (eval роли 3 без `app/agent`) | — |
