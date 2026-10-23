# Роль 4: PostgreSQL, MCP server и REST для Mini App

Вложенные правила для агентов, работающих над частью роли 4. Они дополняют корневой
[`AGENTS.md`](../../AGENTS.md) и не переопределяют его. При конфликте действует корневой файл.
Общение и документация на русском, имена в коде и API на английском.

Перед работой прочитай:
- [`mcp_realisation_plan.md`](mcp_realisation_plan.md): текущий блок, принятые решения, открытые вопросы;
- [`INTEGRATION.md`](INTEGRATION.md): опубликованный контракт для ролей 1, 3, 5;
- корневой `AGENTS.md` §4, §7–§9, §11 (Mini App), §13–§14;
- `docs/backend-contracts.md` (ошибки, доверенный контекст), `docs/record-queries.md`, `docs/idempotency.md`.

## 1. Зона владения

| Моё (роль 4) | Назначение |
| --- | --- |
| `app/mcp_server/` | FastMCP server, схемы инструментов, доверенный контекст, эта документация |
| `app/storage/postgres/` | Настройки, engine/session lifecycle, ORM-таблицы, репозитории PostgreSQL, FTS |
| `migrations/` + `alembic.ini` | Миграции Alembic |
| `app/api/records.py`, `app/api/init_data.py` | REST записей для Mini App и проверка initData MAX |
| `tests/mcp_server/`, `tests/storage_pg/`, `tests/api_records/` | Проверки роли 4 |

**Чужие зоны.** Не меняй их без явного разрешения пользователя:

| Путь | Владелец |
| --- | --- |
| `app/planner/`, `app/storage/redis*.py`, `app/storage/memory.py`, `app/storage/notification_stream.py`, `app/runtime.py`, `app/__main__.py`, `app/workers/`, `app/agent/` | роль 1 |
| `app/bot/` | роль 2 |
| `app/llm/`, `app/asr/`, `app/tts/`, `app/config.py`, `app/metrics.py`, `evals/`, `docs/ML*.md` | роль 3 |
| frontend Mini App | роль 5 |

Изменения в `app/planner/` согласованы как принцип: делает роль 4, **но каждое изменение отдельно
подтверждается пользователем** до правки. Список нужных изменений и их статус ведётся в плане (блок B0).

**Общие файлы.** Разрешены только точечные добавления под роль 4: `pyproject.toml` + `uv.lock`, `compose.yaml`,
`.env.example`, `.github/workflows/backend.yml`, `README.md`. Чужие строки не переписывай. Каждую такую правку
отметь в журнале плана.

## 2. Слои

```text
MCP tool / REST route ─▶ доверенный контекст + Pydantic-схема ─▶ planner service ─▶ Repository protocol ─▶ PostgreSQL
```

- Обработчик инструмента или маршрута только преобразует вход и выход. В нём нет SQL и бизнес-правил.
- Правила (непустое название, `datetime` XOR `date`, обязательное время встречи и напоминания, статусы) живут в
  `app/planner/`. MCP и REST вызывают одни и те же сервисы, без дублирования.
- Репозитории PostgreSQL реализуют существующие протоколы (`TaskRepository`, `MeetingRepository`,
  `ReminderRepository`). Сервисы не импортируют SQLAlchemy.
- Не возвращай ORM-объекты наружу: только DTO из `app/mcp_server/schemas.py`.

## 3. Доверенный контекст

- `owner_id`, `request_id` и `action_id` не входят в аргументы модели и в тело запроса браузера.
- MCP: контекст приходит в заголовках внутреннего вызова от agent worker вместе с сервисным токеном
  (`Authorization: Bearer <MCP_SERVICE_TOKEN>`). Без токена или с неверным токеном вызов отклоняется до planner.
- REST: `owner_id` берётся только из исходной `initData`, проверенной на сервере по алгоритму MAX
  (https://dev.max.ru/docs/webapps/validation), с проверкой свежести. `initDataUnsafe` и `user_id` из браузера
  не подтверждают личность.
- Каждый SQL-запрос фильтрует владельца. Чужая и отсутствующая запись дают одинаковый ответ `not_found`
  или пустой список.

## 4. Время и данные

- Точные моменты хранятся как `TIMESTAMPTZ`, срок задачи без времени как `DATE`. Полночь вместо отсутствующего
  времени не подставляется. Встрече не назначается выдуманная длительность: без `end_time` поле `ends_at` равно `NULL`.
- На вход принимается только ISO 8601 с UTC или явным смещением. Используй `app/planner/time.py`
  (`to_utc`, `day_bounds_utc`, `is_in_period`) и не пиши свои преобразования.
- Периоды задаются полуинтервалами `[start_time, end_time)`.
- Пустая выдача — это `[]`, а не ошибка. Выдача и длины полей ограничены (см. `INTEGRATION.md`).

## 5. Идемпотентность и транзакции

- Ключ — `(owner_id, operation_type, operation_id)`. Отпечаток данных строится через
  `CreationOperation.from_data` (`app/planner/operation.py`). `operation_id` равен `action_id` от роли 1.
- Запись и результат операции фиксируются в одной транзакции. Параллельные повторы разрешает ограничение БД
  (`INSERT … ON CONFLICT`), а не блокировка в Redis.
- Повтор с теми же данными возвращает прежнюю запись. Повтор с другими данными даёт `operation_conflict`.
- Не выполняй HTTP, LLM или ASR внутри транзакции. Используй одну `AsyncSession` на операцию и не разделяй её
  между задачами.

## 6. Ошибки

Коды взяты из `docs/backend-contracts.md`: `validation_error`, `not_found`, `operation_conflict`,
`temporary_unavailable`. Также используется `unauthorized` (транспортный уровень MCP/REST).
Формат ответа: `{"error": {"code", "message", "field"}}`. В сообщениях не должно быть трассировок, DSN,
SQL и чужих данных. Ошибка сохранения никогда не превращается в успешный ответ.

## 7. PostgreSQL, FTS, миграции

- FTS: `to_tsvector('russian', name || ' ' || description)`, GIN-индекс, `websearch_to_tsquery('russian', …)`.
  Это лексический поиск: не называй его семантическим.
- Схема, ORM и миграция должны совпадать. Результат autogenerate проверяй вручную.
  Уже применённые в общем окружении миграции не меняй: создавай следующую.
- Тесты работают только с отдельной тестовой БД. Перед прогоном проверь имя БД, DSN не печатай.
  Общую или production-БД без разрешения не трогай, данные и тома ради теста не удаляй.

## 8. Проверки

Из корня репозитория (manifest один, `pyproject.toml`):

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy          # свои пути добавляются в [tool.mypy].files точечно
RUN_PG_INTEGRATION=1 uv run --locked --env-file .env pytest -q tests/mcp_server tests/storage_pg tests/api_records
```

Интеграционные тесты включаются `RUN_PG_INTEGRATION=1` и подключаются только к `POSTGRES_TEST_DB`
(`tests/storage_pg/__init__.py: integration_settings()` отказывает, если в имени нет `test`).
Локальная БД: `docker compose up -d --wait postgres` (порт 5433). Без живого PostgreSQL честно сообщай,
что интеграционные тесты пропущены.
Для проверки части роли 4 можно вызвать субагента `mcp-tester` (`.claude/agents/mcp-tester.md`, локальный файл).

## 9. Изменение контракта

Изменилось имя или схема инструмента, DTO, маршрут REST или формат ошибки? Тогда в одном блоке:
1. обнови `INTEGRATION.md`, подними версию и допиши журнал;
2. обнови `mcp_realisation_plan.md` (решения и журнал архитектуры);
3. в отчёте перечисли, что должны сделать роли 1, 3, 5.

Изменилась архитектура? Сначала обнови план, потом код.
