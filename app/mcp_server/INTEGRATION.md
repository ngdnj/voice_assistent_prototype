# Интеграция с ролью 4: MCP-инструменты и REST записей

> **Статус: v3 — MCP и REST реализованы** (`app/mcp_server/`, запуск `python -m app.mcp_server`).
> REST для Mini App (раздел 4) реализован в том же процессе. Изменения — только через журнал версий внизу.

Документ для разработчиков и их агентов: ролей 1 (agent runner, MCP client), 3 (промпт, схемы tools),
5 (Mini App). Внутреннее устройство роли 4 здесь не описано, см. [`MCP_REALISATION_PLAN.md`](MCP_REALISATION_PLAN.md).

## 0. Что сделать каждой роли

| Роль | Что нужно сделать для интеграции | Раздел |
| --- | --- | --- |
| 1 | Передавать заголовки доверенного контекста, один `action_id` на действие; решить, где монтировать MCP/REST; перевести runtime на PostgreSQL | 3, «Запуск» |
| 2 | Класть в `X-Owner-Id` строку с числовым id пользователя MAX (`str(user.id)`); дать `MAX_BOT_TOKEN` для проверки `initData` | 4 |
| 3 | Сверить схемы tools и промпт с разделом 3 (ошибки-объекты, нет `already_exists`, `end_time`, выборка задач по сроку) | 3 |
| 5 | Ходить в `/api/*` с заголовком `X-Max-Init-Data`, передавать `timezone`, обрабатывать 401/404/422/503 | 4 |
| 6 | Строить панели по метрикам из раздела 5 | 5 |

### Запуск локально

```bash
docker compose up -d --wait postgres
uv run --locked --env-file .env alembic upgrade head
uv run --locked --env-file .env python -m app.mcp_server   # MCP: /mcp, REST: /api/*, метрики: /metrics/
```

Переменные — в `.env.example`: `POSTGRES_*`, `MCP_SERVICE_TOKEN` (≥ 32 символов), `MCP_HOST`, `MCP_PORT`,
`MCP_PATH`, `MAX_BOT_TOKEN` (включает REST), `MINI_APP_ORIGINS` (CORS).

## 1. Общие правила

- Владельца записи определяет сервер. В аргументах инструментов и в телах REST **нет** `user_id`/`owner_id`;
  если их передать, они игнорируются или приводят к `validation_error`.
- Время: ISO 8601 с `Z` или явным смещением (`2026-10-24T15:00:00+03:00`). Время без смещения даёт `validation_error`.
  В ответах время в UTC (`...Z`), дата без времени — `YYYY-MM-DD`.
- Периоды — полуинтервал `[start_time, end_time)`: «на сегодня» = `[сегодня 00:00, завтра 00:00)` в зоне пользователя.
  Границы локальных суток вычисляет вызывающая сторона (роль 3/1 по `timezone`), сервер принимает моменты.
- Пустая выдача: `{"events": []}`. Это не ошибка.
- Поиск — PostgreSQL FTS (русская морфология, лексический), не семантический.

## 2. DTO `Event`

```json
{
  "id": "6f1c2c8e-0b7a-4c3e-9d1e-2b5b8f0c9a11",
  "event_type": "task",
  "name": "Подготовить отчёт",
  "description": "",
  "status": "todo",
  "date": "2026-10-24",
  "datetime": null,
  "end_time": null,
  "created_at": "2026-10-23T09:30:00Z"
}
```

| Поле | Тип | Правило |
| --- | --- | --- |
| `id` | UUID | |
| `event_type` | `task` \| `meeting` \| `reminder` | |
| `name` | string 1–200 | крайние пробелы удаляются |
| `description` | string 0–2000 | по умолчанию `""` |
| `status` | `todo` \| `in_progress` \| `done` \| `null` | только у `task`, у остальных `null` |
| `date` | `YYYY-MM-DD` \| `null` | только у `task`: срок без времени |
| `datetime` | ISO 8601 \| `null` | `task`: срок со временем (необязателен); `meeting`, `reminder`: обязательный момент |
| `end_time` | ISO 8601 \| `null` | только у `meeting`, `> datetime`; без него длительность неизвестна |
| `created_at` | ISO 8601 UTC | |

## 3. MCP

### Подключение (роль 1)

- Транспорт: streamable HTTP. Пока роль 1 не решила, где монтировать MCP в общем API (Q1 в плане),
  сервер запускается отдельным процессом:

```bash
MCP_SERVICE_TOKEN=<не меньше 32 символов> uv run --locked --env-file .env python -m app.mcp_server
# слушает http://127.0.0.1:8001/mcp (MCP_HOST, MCP_PORT, MCP_PATH), хранилище — PostgreSQL из POSTGRES_*
```

- Для сборки внутри другого процесса: `build_mcp_server(build_tools(sessions), header_context(token))`
  из `app/mcp_server/server.py` и `app/mcp_server/__main__.py`; ASGI-приложение — `mcp.http_app(path="/mcp")`.
- Заголовки каждого вызова ставит agent runner, не LLM. Значения длиннее 200 символов отклоняются.

| Заголовок | Обязателен | Значение |
| --- | --- | --- |
| `Authorization` | да | `Bearer <MCP_SERVICE_TOKEN>` — общий секрет worker и MCP, сравнивается за постоянное время |
| `X-Owner-Id` | да | доверенный владелец из нормализованного сообщения MAX |
| `X-Timezone` | да | IANA-зона пользователя из профиля (`Europe/Moscow`); нужна для сроков-дат в периоде |
| `X-Action-Id` | для `create_event` | `action_id` логического действия; **неизменен при retry job и повторном tool call того же действия** |
| `X-Request-Id` | нет | `request_id` для логов; без него сервер создаёт свой |

Без токена, владельца или зоны любой инструмент возвращает `unauthorized`, planner не вызывается.

Пример:

```python
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport(
    settings.mcp_url,
    headers={
        "Authorization": f"Bearer {settings.mcp_service_token}",
        "X-Owner-Id": job.user_id,
        "X-Timezone": job.timezone,
        "X-Request-Id": job.request_id,
        "X-Action-Id": action_id,
    },
)
async with Client(transport) as mcp:
    result = await mcp.call_tool("create_event", {"event_type": "task", "name": "Отчёт", "date": "2026-10-24"})
```

Вопрос к роли 1: для нескольких `create_event` в одном ходе нужен свой `action_id` на каждый вызов.
Предложение — `action_id = f"{job_id}:{номер вызова}"`, это детерминировано при повторе job.

### Формат ошибки

Ошибки возвращаются **в результате инструмента** (не исключением MCP), чтобы агент видел JSON. Это касается и
аргументов, не прошедших схему (нет `name`, неверный тип, лишнее поле вроде `user_id`): `validation_error`
с именем поля, без текста валидатора.

```json
{"error": {"code": "validation_error", "message": "Для встречи нужно точное время", "field": "datetime"}}
```

| `code` | Когда | Что делать агенту |
| --- | --- | --- |
| `validation_error` | неверные/недостающие аргументы | уточнить у пользователя, не повторять тот же вызов |
| `operation_conflict` | тот же `action_id` с другими данными **или для записи другого типа** | не повторять; ошибка программы роли 1 |
| `not_found` | запись не найдена у этого владельца | сообщить об отсутствии |
| `temporary_unavailable` | БД недоступна | ограниченный retry с тем же `action_id` |
| `unauthorized` | нет/неверный токен или контекст | ошибка конфигурации, не показывать пользователю детали |

### `create_event`

Создать задачу, встречу или напоминание.

| Аргумент | Тип | Обязателен | Правило |
| --- | --- | --- | --- |
| `event_type` | `task` \| `meeting` \| `reminder` | да | |
| `name` | string 1–200 | да | |
| `description` | string 0–2000 | нет | |
| `date` | `YYYY-MM-DD` | нет | только `task`; взаимоисключается с `datetime` |
| `datetime` | ISO 8601 со смещением | для `meeting`, `reminder` | у `task` необязателен |
| `end_time` | ISO 8601 со смещением | нет | только `meeting`, позже `datetime` |

Успех: `{"created": Event}`. Повтор того же `action_id` с теми же данными возвращает **ту же** запись
в текущем состоянии (тот же `id` и `created_at`), второй записи нет — в том числе при параллельных
повторах. Флага «повтор» нет: для агента результат одинаков. Тот же `action_id` с другими данными —
`operation_conflict`. Напоминание в прошлом — `validation_error`, `field: "datetime"`.
Хранилище недоступно — `temporary_unavailable` без `created`: подтверждать создание нельзя.

### `get_events_by_period`

| Аргумент | Тип | Обязателен | Правило |
| --- | --- | --- | --- |
| `start_time` | ISO 8601 | да | |
| `end_time` | ISO 8601 | да | `> start_time`, период ≤ 366 дней |
| `event_types` | список типов | нет | по умолчанию все |

Результат: `{"events": [Event, ...], "truncated": bool}`, до 100 записей, по возрастанию времени
(`truncated: true` — записей может быть больше, сузьте период или типы). Что попадает в период:
- `meeting` — по началу (`datetime`); встреча, начавшаяся раньше периода, не попадает;
- `reminder` — по `datetime`;
- `task` — по сроку: `datetime` по моменту; `date` — если местная полночь этой даты (по `X-Timezone`)
  лежит в периоде. Для периода «сегодня» `[00:00, 24:00)` задача на сегодня попадает, для «с 15 до 18» — нет.
  Задачи без срока в период не попадают (их находит `get_event_by_query`).
Срок-дата сортируется по своей местной полуночи.

### `get_event_by_query`

| Аргумент | Тип | Обязателен | Правило |
| --- | --- | --- | --- |
| `query` | string 1–200 | да | слова из названия/описания |
| `event_types` | список типов | нет | |
| `start_time`, `end_time` | ISO 8601 | нет | сужают поиск, полуинтервал |
| `limit` | int 1–50 | нет | по умолчанию 10 |

Результат: `{"events": [Event, ...]}` по возрастанию времени записи (не по релевантности): срок задачи,
начало встречи, время напоминания; задача без срока — по времени создания. Порядок точен в пределах
50 совпадений каждого типа. Ничего не найдено: `{"events": []}`.
Поиск — PostgreSQL FTS (`websearch_to_tsquery('russian', …)`) по словам с морфологией: «бюджета» находит
«бюджет», «встречу» — «Встреча». Части слов («отч») не находятся. С периодом задачи отбираются по сроку,
как в `get_events_by_period`; без периода — все задачи, включая без срока.

### Для роли 3: отличия от описанной в `docs/ML*.md` заглушки

- Добавлены `description` и `end_time` (встреча). Длительность встречи не придумывать.
- Формат ошибки — объект `{"error": {"code", "message", "field"}}`, а не строка. `ToolCall.ok` (`"error" not in result`) остаётся корректным.
- Нет `already_exists`: повтор того же действия возвращает ту же запись (`{"created": ...}`).
- Схемы аргументов содержат `anyOf [X, null]` для необязательных полей — их по-прежнему нужно упрощать для
  GigaChat (`app/agent/tools.py` у роли 3). Перечисления (`event_type`, элементы `event_types`) заданы `enum`.
- Временная опора и зона: `reference_time` остаётся в промпте агента; MCP принимает уже вычисленные моменты.
- `delete_event` не входит в MVP.
- У встреч и напоминаний `status = null` — совпадает с заглушкой.

## 4. REST для Mini App (роль 5)

Реализовано в `app/api/records.py`; тот же процесс, что и MCP (`python -m app.mcp_server`), маршруты `/api/*`.
REST включается, только если задан `MAX_BOT_TOKEN` (токен бота MAX, которым подписана `initData`).

**Авторизация.** Каждый запрос несёт исходную строку `window.WebApp.initData` в заголовке
`X-Max-Init-Data` (имя выбрано нами: документация MAX заголовок не задаёт). Сервер проверяет подпись по
[алгоритму MAX](https://dev.max.ru/docs/webapps/validation) и свежесть: `auth_date` не старше суток и не
больше чем на минуту в будущем. `initDataUnsafe`, `user_id` в параметрах или теле не используются.
Владелец — `str(user.id)` из проверенной `initData`: **роль 2 должна передавать в `X-Owner-Id` MCP то же
значение** (строка с числовым id пользователя MAX), иначе бот и Mini App увидят разные записи.
Без валидной `initData` любой маршрут отвечает `401` раньше проверки параметров.

| Метод и путь | Параметры | Ответ `200` |
| --- | --- | --- |
| `GET /api/events` | `start_time`, `end_time`, `timezone`, `event_types` (повторяемый: `?event_types=task&event_types=reminder`) | `{"events": [Event], "truncated": bool}` |
| `GET /api/events/search` | `query`, `timezone`, необязательные `event_types`, `start_time`, `end_time`, `limit` (1–50) | `{"events": [Event]}` |
| `PATCH /api/tasks/{id}/status` | тело `{"status": "todo" \| "in_progress" \| "done"}` | `{"event": Event}` |

- Правила периода, поиска и сортировки — как у `get_events_by_period` и `get_event_by_query` (раздел 3):
  REST вызывает тот же код. Вкладки Mini App — фильтр `event_types`.
- `timezone` — IANA-зона, в которой Mini App показывает время (например, из `Intl.DateTimeFormat().resolvedOptions().timeZone`
  или настройки пользователя); нужна для сроков-дат. На доступ не влияет.
- Время в ответах — UTC (`...Z`); локальное время показывает клиент.
- Статус меняется только у задач. Встреча, чужая или несуществующая задача — `404`.

| HTTP | `error.code` | Когда |
| --- | --- | --- |
| `401` | `unauthorized` | нет, неверная или просроченная `initData` — открыть приложение из MAX заново |
| `404` | `not_found` | запись не найдена у этого пользователя (в том числе чужая) |
| `422` | `validation_error` | неверные параметры; `field` — имя параметра |
| `503` | `temporary_unavailable` | хранилище недоступно, изменение не выполнено |

Тело ошибки — тот же формат `{"error": {"code", "message", "field"}}`. Повторять `PATCH` безопасно:
тот же статус ничего не меняет. Ответы на устаревшие запросы UI должен отбрасывать сам (AGENTS §11).
CORS: разрешённые origin Mini App задаются `MINI_APP_ORIGINS` (через запятую); без него CORS выключен.
Процесс также отдаёт `/metrics/` и `/mcp`: при публикации через reverse proxy наружу открывать только `/api/`.

Пример запроса из Mini App:

```ts
const response = await fetch(`${API_URL}/api/events?` + new URLSearchParams({
  start_time: "2026-10-24T00:00:00+03:00",
  end_time: "2026-10-25T00:00:00+03:00",
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
}), { headers: { "X-Max-Init-Data": window.WebApp.initData } });
if (response.status === 401) { /* попросить открыть приложение из MAX заново */ }
const body = await response.json(); // {"events": [...], "truncated": false} или {"error": {...}}
```

## 5. Метрики (роль 6)

`GET /metrics/` в формате Prometheus. Метки — только перечисления, без пользователей и текстов.

| Метрика | Тип | Метки |
| --- | --- | --- |
| `assistant_record_operations_total` | counter | `operation` (`create_event`, `get_events_by_period`, `get_event_by_query`, `update_task_status`), `transport` (`mcp`, `rest`), `outcome` (`ok` или код ошибки; `exception` — непредвиденная ошибка) |
| `assistant_record_operation_seconds` | histogram | `operation`, `transport` |

Примеры: доля ошибок хранилища — `sum(rate(assistant_record_operations_total{outcome="temporary_unavailable"}[5m]))`;
p95 длительности — `histogram_quantile(0.95, sum by (le, operation) (rate(assistant_record_operation_seconds_bucket[5m])))`.

## Журнал версий

| Версия | Дата | Изменение |
| --- | --- | --- |
| v0 | 2026-10-23 | Первое предложение контракта: три инструмента, DTO, формат ошибок, REST Mini App |
| v1 | 2026-10-23 | MCP реализован. Добавлен обязательный `X-Timezone`; убран флаг `replayed`; порядок поиска — по времени; описана выборка задач по сроку |
| v2 | 2026-10-23 | REST для Mini App реализован: заголовок `X-Max-Init-Data`, свежесть сутки, `timezone` в запросе, ответ PATCH — `{"event": ...}`, владелец — `str(user.id)` |
| v3 | 2026-10-23 | По итогам проверки: `action_id` уникален для владельца независимо от типа записи; ошибки схемы — структурированные; `date` строго `YYYY-MM-DD`; NUL в тексте — `validation_error`; уточнён порядок поиска |
| v3.1 | 2026-10-23 | Без изменения контракта: задачи по ролям, локальный запуск, пример запроса Mini App, метрики для роли 6 |
