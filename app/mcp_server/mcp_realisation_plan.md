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
| D6 | Идемпотентность — отдельная таблица `operations` с PK `(owner_id, operation_id)` (изменено 2026-10-23 по итогам проверки: был ключ с `operation_type`, и тот же `action_id` для другого типа создавал вторую запись); тип и отпечаток сверяются при повторе; вставка в одной транзакции с записью. Redis-адаптеры роли 1 по-прежнему различают тип | AGENTS §9, `docs/idempotency.md` |
| D7 | `delete_event` не входит в MVP; инструментов три | AGENTS §8, ML_HANDOFF T2 |
| D8 | Внешнее `name` соответствует `Task.title` и `Reminder.text`. Маппинг делается в адаптере, поля planner не переименовываются | AGENTS §7 (последний абзац) |
| D9 | Ошибки инструментов возвращаются в результате (`{"error": {...}}`), а не исключением MCP: агенту роли 3 нужен JSON (`ToolCall.ok`) | `docs/ML.md` |
| D10 | Одна колонка момента `scheduled_at` вместо `starts_at`: начало встречи, время напоминания, срок `due_at` задачи — один индекс для периода; маппинг в репозиториях | роль 4, 2026-10-23 |

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

Риски из проверки mcp-tester (2026-10-23):
- новые Redis-записи всегда содержат `description`; код до B0 (например, `main` у роли 1 на том же Redis) их не прочитает. Нужно сообщить роли 1, откат на старый код с тем же Redis не поддерживается;
- `update_status(todo)` у старой записи `active` не перезаписывает её (читается как `todo`; нормализуется при следующей смене статуса).

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

### B2. Схема и миграции ✅ 2026-10-23 (интеграция на Compose PostgreSQL 18.6)

Таблица `events`:

| Колонка | Тип | Ограничения |
| --- | --- | --- |
| `id` | `UUID` PK | |
| `owner_id` | `TEXT` | NOT NULL, не пустая |
| `event_type` | `TEXT` | `IN ('task','meeting','reminder')` |
| `name` | `TEXT` | `btrim(name) <> ''` (лимит 200 — в схемах MCP/REST, см. Q8) |
| `description` | `TEXT` | NOT NULL DEFAULT `''`, длина ≤ 2000 |
| `status` | `TEXT NULL` | у `task` — `IN ('todo','in_progress','done')`, у остальных `NULL` |
| `due_date` | `DATE NULL` | только у `task` |
| `scheduled_at` | `TIMESTAMPTZ NULL` | момент записи: начало встречи, время напоминания, `due_at` задачи; `NOT NULL` у meeting/reminder; XOR с `due_date` |
| `ends_at` | `TIMESTAMPTZ NULL` | только у meeting, `> scheduled_at` |
| `created_at` | `TIMESTAMPTZ` | NOT NULL |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL |
| `search` | `tsvector` GENERATED STORED | `to_tsvector('russian', name \|\| ' ' \|\| description)` |

Таблица `operations`: `owner_id`, `operation_type`, `operation_id` (PK), `fingerprint CHAR(64)`,
`event_id UUID FK events`, `created_at`.

Индексы: `(owner_id, scheduled_at, id)`, `(owner_id, due_date, id)`, `(owner_id, event_type, created_at, id)`, `GIN(search)`.
Напоминание: дополнительно `CHECK (scheduled_at >= created_at)` — то же правило, что `PastReminderTimeError`.

- Alembic в `migrations/`, `alembic.ini` в корне, async env. Первая миграция `0001` написана вручную; имена
  ограничений через `op.f()` (иначе naming convention metadata добавляет префикс второй раз).
- `env.py` берёт адрес из `config.attributes["postgres_settings"]` (тесты) или `PostgresSettings.from_env()`.
- Шаблон `script.py.mako` приведён к стилю ruff (`str | Sequence[str] | None`).
- Тесты (`tests/storage_pg/test_migrations.py`): SQL офлайн без БД; upgrade/downgrade/upgrade; `compare_metadata`
  без расхождений; имена CHECK совпадают с metadata; 15 неверных строк `events` и 5 неверных `operations`
  отклоняются; FTS по-русски («встречу», «бюджета», «молока»), фильтр владельца.

### B3. Репозитории PostgreSQL ✅ 2026-10-23

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

Сделано: общий `PostgresEventStore[R]` (`app/storage/postgres/records.py`) + три тонких репозитория с маппингом
полей (D8/D10). Отличия от первоначального плана, принятые в B3:
- порядок поиска — `(время, UUID)`, как требует протокол (`list` «in stable time/ID order»), а не `ts_rank`;
  ранжирование по релевантности — при необходимости в B4 на уровне `get_event_by_query`;
- период задач — по `created_at`, как в `RecordQuery`-контракте и Redis (см. Q2): фильтр по сроку требует
  расширения `RecordQuery` в planner — отдельное разрешение в B4;
- `updated_at` пишет часы репозитория задач (`clock`, подменяемы в тестах); повтор того же статуса ничего не пишет.
Тесты: `tests/storage_pg/test_repositories.py` — 11 (1 без БД: недоступная БД → `*StorageError`).

### B4. MCP server ✅ 2026-10-23

- Перед кодом сверить API FastMCP 4.1.3 (https://gofastmcp.com/): объявление инструментов, доступ к HTTP-заголовкам
  из контекста, streamable HTTP, монтирование в ASGI.
- `app/mcp_server/schemas.py`: Pydantic-модели аргументов, `EventDTO`, `ToolError`.
- `app/mcp_server/context.py`: извлечение и проверка доверенного контекста (сервисный токен через
  `hmac.compare_digest`, `owner_id`, `action_id`, `request_id`).
- `app/mcp_server/server.py`: `build_mcp_server(services) -> FastMCP`, три инструмента, сборка без сети при импорте.
- Тесты: in-memory клиент FastMCP. Проверяются схемы инструментов (нет `user_id` в аргументах), валидация,
  отсутствие или неверный токен, повтор `action_id`, пустая выдача, ошибка хранилища → `temporary_unavailable`
  без ложного `created`.

Сделано:
- planner (с разрешения): `RecordQuery.task_period` (`created_at` | `due`) и `timezone`; срок-дата попадает в период,
  если её местная полночь в `[start, end)` (`RecordQuery.due_date_bounds`). PostgreSQL реализует `due`
  (`OR` по `scheduled_at`/`due_date`, сортировка по `COALESCE(scheduled_at, местная полночь)`); Redis и in-memory
  явно отказывают (`ValueError`) вместо неверного ответа. Лимит названия 200 символов — в `create_*` (восстановление
  старых длинных записей не ломается); `CHECK char_length(name) <= 200` добавлен в миграцию 0001 (она применялась
  только к локальной пустой БД — переприменена, а не создана 0002).
- `app/mcp_server/context.py` (заголовки, сервисный токен, `hmac.compare_digest`), `schemas.py` (`EventDTO`, ошибки),
  `tools.py` (`EventTools`: разбор аргументов, вызов planner, маппинг ошибок, лимиты), `server.py`
  (`build_mcp_server`, описания инструментов, `enum` без валидации FastMCP — ошибка остаётся структурированной),
  `__main__.py` (`McpSettings`, PostgreSQL engine на процесс, `run_http_async`).
- D11: обязательный заголовок `X-Timezone` — нужен для сроков-дат; D12: без флага `replayed`; поиск сортируется по времени.
- Тесты: `tests/mcp_server/test_context.py`, `test_tools.py` (in-memory клиент FastMCP), `test_integration.py`
  (PostgreSQL + настоящий HTTP через uvicorn на 127.0.0.1 с проверкой токена). Ручной запуск процесса: ответ по HTTP,
  токен не попадает в лог, SIGTERM — штатная остановка uvicorn.
- Ограничение: uvicorn после штатной остановки повторно поднимает SIGTERM, поэтому выход `async with postgres_engine`
  (dispose пула) не выполняется — соединения закрывает ОС. Исправить при встраивании в общий lifecycle роли 1.

### B5. REST для Mini App ✅ 2026-10-23

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

Сделано:
- `app/api/init_data.py`: проверка по документации MAX (dev.max.ru/docs/webapps/validation, сверено 2026-10-23):
  каждая пара ровно один раз, URL-декодирование, сортировка, `\n`, ключ `HMAC-SHA256("WebAppData", bot_token)`,
  hex, `hmac.compare_digest`; затем свежесть и целочисленный `user.id` → `owner_id = str(user.id)`.
- `app/api/records.py`: `build_records_router(tools, bot_token, clock)`; REST вызывает те же `EventTools`, что MCP
  (+ `EventTools.update_task_status` — только REST). Параметры необязательны для FastAPI, чтобы 401 шёл раньше 422.
- `app/mcp_server/__main__.py`: один FastAPI-процесс — `/api/*` и MCP (`mcp.http_app` смонтирован в корень, его
  lifespan у FastAPI); REST включается только с `MAX_BOT_TOKEN`; CORS по `MINI_APP_ORIGINS`.
- Зависимость `fastapi>=0.161,<0.162` (0.161.1).
- Тесты: `tests/api_records/test_init_data.py` (подпись, свежесть, 12 подделок), `test_records_api.py` (PostgreSQL:
  401 до валидации, период/вкладки/поиск записей, созданных через инструменты агента, смена статуса видна агенту,
  404/422, чужой пользователь). Ручной запуск: REST 401/200 и MCP на одном порту, секреты не в логе.
- Требование к роли 2 (записано в INTEGRATION): `X-Owner-Id` = `str(MAX user id)`, иначе бот и Mini App разойдутся.

### B6. Интеграция ◐ (своя часть ✅ 2026-10-23; ждёт других ролей)

- ✅ CI: сервис `postgres:18-alpine` (`it_gorky_test`, порт 5433), `RUN_PG_INTEGRATION=1`, `alembic upgrade head`,
  `alembic check` в `.github/workflows/backend.yml`. Шаги проверены локально на чистом контейнере с теми же env.
- ✅ README: разделы PostgreSQL, MCP server, REST для Mini App.
- ✅ `INTEGRATION.md` v2 с отличиями от ML-заглушки.
- ☐ Подключение в общий API и runtime — у роли 1 (см. «Что нужно от других ролей»).

### B7. Метрики ✅ 2026-10-23

AGENTS §15: каждый владелец добавляет показатели своего компонента. `app/mcp_server/metrics.py`:
- `assistant_record_operations_total{operation, transport, outcome}` — `outcome`: `ok` или код ошибки
  (`temporary_unavailable` = недоступность PostgreSQL), `exception` — непредвиденная ошибка;
- `assistant_record_operation_seconds{operation, transport}` — длительность с учётом PostgreSQL.
Метки — только ограниченные перечисления, без пользователей, текстов и id. MCP-инструменты и REST-маршруты
обёрнуты `measured`; процесс отдаёт `/metrics/` (prometheus_client). Сбор и Grafana — роли 1 и 6.

### Проверка mcp-tester B2–B7 (2026-10-23) ✅

8 подтверждённых дефектов исправлены, каждый закреплён тестом (`tests/mcp_server/test_edge_cases.py`,
`tests/api_records/test_edge_cases.py`, раньше помечены `expectedFailure`):
1. тот же `action_id` с другим `event_type` создавал вторую запись → PK `operations` = `(owner_id, operation_id)`,
   при повторе сверяются тип и отпечаток (миграция 0001 не публиковалась — дополнена, БД пустые, переприменена);
2. аргументы, не прошедшие схему FastMCP, давали ошибку транспорта → middleware `StructuredArgumentErrors`;
3. NUL в тексте давал `temporary_unavailable` (агенту предписан бесполезный retry) → `validation_error` в `EventTools`;
4. зоны, где DST пропускает/повторяет полночь (Гавана, Сантьяго), ломали период и поиск → `local_day_start_utc`
   (fold=0, без отказа) в `query.py` и `EventDTO.sort_key`; для повторённой полуночи сортировка в SQL может
   отличаться на час от Python, итоговый порядок задаёт Python;
5. `limit=²` давал 500 → только ASCII-цифры;
6. PATCH с битым телом без initData давал 422 FastAPI → тело читается после проверки initData;
7. поиск с `limit` без периода отбирал задачи по созданию → каждый тип читается до 50, обрезка после общей сортировки;
8. `date` принимал ISO-неделю → строго `YYYY-MM-DD` (ASCII-цифры).
Замечания: `/metrics/` и `/mcp` на том же порту, что `/api` — при публикации открывать наружу только `/api/`
(записано в INTEGRATION); шапка INTEGRATION приведена к версии v3.

## Что нужно от других ролей

| Роль | Что | Зачем |
| --- | --- | --- |
| 1 | Решить Q1: смонтировать `build_records_router` и MCP (`build_app` в `app/mcp_server/__main__.py` — образец) в общий API | сейчас роль 4 запускает отдельный процесс |
| 1 | Перевести `app/runtime.py` на PostgreSQL-репозитории; решить Q3 (перенос данных из Redis) | Redis — временное хранилище прототипа |
| 1 | MCP client: заголовки `Authorization`, `X-Owner-Id`, `X-Timezone`, `X-Action-Id` (свой на каждый `create_event`, неизменный при retry), `X-Request-Id` | доверенный контекст и идемпотентность |
| 1 | Встроить процесс в общий lifecycle: dispose пула PostgreSQL при SIGTERM | uvicorn повторно поднимает SIGTERM, `async with` не завершается |
| 1 | Предупреждение: код до B0 (`main`) не читает новые Redis-записи с `description` | общий Redis при откате |
| 2 | `X-Owner-Id` = `str(user_id MAX)` в нормализованном сообщении | бот и Mini App должны видеть одни записи |
| 2 | Предоставить `MAX_BOT_TOKEN` процессу роли 4 (только сервер) | проверка подписи `initData` |
| 3 | Сверить промпт и схемы с `INTEGRATION.md` v2: формат ошибок, `end_time`, нет `replayed`/`already_exists`/`delete_event`, задачи без срока не входят в период | ML-заглушка описывала предварительный контракт |
| 5 | REST по разделу 4 `INTEGRATION.md`: `X-Max-Init-Data`, `timezone`, `event_types`, `PATCH .../status`, обработка 401/404/422/503 | Mini App |
| 6 | Панели Grafana по `assistant_record_operations_total` и `assistant_record_operation_seconds` | защита |

## Открытые вопросы

| № | Вопрос | Кому | Статус |
| --- | --- | --- | --- |
| Q1 | Где монтируются MCP и REST: общий FastAPI в `app/api/main.py` (роль 1) или отдельный процесс роли 4 на время разработки | роль 1 | открыт; временно отдельный процесс `python -m app.mcp_server` на 8001 |
| Q2 | Попадают ли задачи без срока в `get_events_by_period`. Предложение: нет (фильтр по сроку), для «все задачи» — отдельный флаг `include_undated` | роли 3, 5 | ✅ 2026-10-23: задачи без срока в период не попадают; `RecordQuery.task_period="due"` (разрешение пользователя) |
| Q3 | Нужен ли перенос данных из Redis в PostgreSQL или прототипные данные можно не переносить | роль 1 | открыт |
| Q4 | Задача с `due_at` (точное время): разрешаем или только `date`? AGENTS §8 допускает `datetime` для срока | роль 3 | ✅ разрешаем (пользователь, 2026-10-23, B0.3) |
| Q5 | Точное имя заголовка и формат передачи `initData` из Mini App | роль 5 + документация MAX | ✅ 2026-10-23: `X-Max-Init-Data` с исходной `window.WebApp.initData` (документация MAX имя не задаёт) |
| Q6 | Окно свежести `auth_date` (предложение: 24 ч) | роль 5 | ✅ сутки (пользователь, 2026-10-23); будущее — не больше 1 мин |
| Q7 | Ответ `create_event` при повторе: тот же `{"created": …}` или пометка `"replayed": true` | роль 3 | ✅ 2026-10-23: тот же `{"created": ...}` без флага (сервис не различает повтор; агенту не нужно) |
| Q8 | В planner нет лимита длины названия; контракт MCP — 1–200. Добавить лимит в planner (нужно разрешение) или только в схемах MCP/REST | пользователь | ✅ 2026-10-23: лимит 200 в planner при создании (разрешение), `CHECK` в БД |

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
| 2026-10-23 | Проверка mcp-tester: дефектов нет; ужесточена защита имени тестовой БД, регрессионный тест старых Redis-данных | `tests/storage_pg/`, `tests/test_redis_tasks.py` |
| 2026-10-23 | B2: схема `events`/`operations`, миграция 0001, D10 `scheduled_at`, Q8 открыт; локальная `it_gorky` мигрирована до 0001; 189 passed | `app/storage/postgres/tables.py`, `alembic.ini`, `migrations/`, `tests/storage_pg/test_migrations.py`, `pyproject.toml`, `README.md` |
| 2026-10-23 | B3: репозитории PostgreSQL для трёх типов; 200 passed (с Redis и PostgreSQL) | `app/storage/postgres/{records,tasks,meetings,reminders}.py`, `tests/storage_pg/test_repositories.py`, `README.md` |
| 2026-10-23 | B4: MCP server; planner: `RecordQuery.task_period`/`timezone`, лимит названия 200; INTEGRATION v1; 221 passed | `app/mcp_server/*.py`, `app/planner/{query,fields,task,meeting,reminder}.py`, `app/storage/{memory,redis_tasks}.py`, `app/storage/postgres/{records,tasks,tables}.py`, `migrations/versions/0001_*.py`, `tests/mcp_server/`, `tests/test_record_query_due.py`, `.env.example`, `pyproject.toml`, `README.md` |
| 2026-10-23 | B5: REST Mini App + initData; MCP и REST в одном процессе; INTEGRATION v2; 232 passed | `app/api/{init_data,records}.py`, `app/mcp_server/{__main__,tools}.py`, `tests/api_records/`, `pyproject.toml`, `uv.lock`, `.env.example`, `README.md` |
| 2026-10-23 | B6 (своя часть): CI с PostgreSQL; B7: метрики роли 4 и `/metrics/`; список задач для других ролей | `.github/workflows/backend.yml`, `app/mcp_server/{metrics,server,__main__}.py`, `app/api/records.py`, `tests/mcp_server/test_metrics.py`, план |
| 2026-10-23 | Исправлены 8 дефектов проверки; D6 изменён (PK `operations` без типа); INTEGRATION v3; 253 passed | `app/storage/postgres/{tables,records}.py`, `migrations/versions/0001_*.py`, `app/mcp_server/{server,tools,schemas}.py`, `app/planner/query.py`, `app/api/records.py`, `tests/*/test_edge_cases.py`, `tests/storage_pg/test_repositories.py` |
