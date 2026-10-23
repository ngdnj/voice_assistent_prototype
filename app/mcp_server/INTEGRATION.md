# Интеграция с ролью 4: MCP-инструменты и REST записей

> **Статус: v0 — предложение, не реализовано.** Контракт можно обсуждать и менять до v1.
> Код по нему пока не пишется на стороне сервера; клиентские заглушки ролей 1, 3, 5 стоит
> держать совместимыми с этим документом. Изменения — только через журнал версий внизу.

Документ для разработчиков и их агентов: ролей 1 (agent runner, MCP client), 3 (промпт, схемы tools),
5 (Mini App). Внутреннее устройство роли 4 здесь не описано, см. [`mcp_realisation_plan.md`](mcp_realisation_plan.md).

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

- Транспорт: streamable HTTP, URL из `MCP_URL` (например `http://localhost:8001/mcp`; итоговый путь фиксируется в B4/B6).
- Обязательные заголовки каждого вызова (их ставит agent runner, не LLM):

| Заголовок | Значение |
| --- | --- |
| `Authorization` | `Bearer <MCP_SERVICE_TOKEN>` — секрет из окружения, общий для worker и API |
| `X-Owner-Id` | доверенный владелец из нормализованного сообщения MAX |
| `X-Request-Id` | `request_id` для логов |
| `X-Action-Id` | `action_id` логического действия; **неизменен при retry job и повторном tool call того же действия** |

Пример (точный API передачи заголовков сверить с FastMCP 4.1.3 при B4):

```python
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport(
    settings.mcp_url,
    headers={
        "Authorization": f"Bearer {settings.mcp_service_token}",
        "X-Owner-Id": job.user_id,
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

Ошибки возвращаются **в результате инструмента** (не исключением MCP), чтобы агент видел JSON:

```json
{"error": {"code": "validation_error", "message": "Для встречи нужно точное время", "field": "datetime"}}
```

| `code` | Когда | Что делать агенту |
| --- | --- | --- |
| `validation_error` | неверные/недостающие аргументы | уточнить у пользователя, не повторять тот же вызов |
| `operation_conflict` | тот же `action_id` с другими данными | не повторять; ошибка программы роли 1 |
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

Успех: `{"created": Event, "replayed": false}`. Повтор того же `action_id` с теми же данными:
`{"created": <та же запись>, "replayed": true}`. Второй записи не создаётся.
Напоминание в прошлом даёт `validation_error`, `field: "datetime"`.

### `get_events_by_period`

| Аргумент | Тип | Обязателен | Правило |
| --- | --- | --- | --- |
| `start_time` | ISO 8601 | да | |
| `end_time` | ISO 8601 | да | `> start_time`, период ≤ 366 дней |
| `event_types` | список типов | нет | по умолчанию все |

Результат: `{"events": [Event, ...], "truncated": false}`, до 100 записей, по возрастанию времени.
Время записи для периода: `meeting`/`reminder` — `datetime` (встреча попадает в период по началу);
`task` — срок: `datetime` по моменту; `date` попадает, если дата лежит в периоде (как сравнивать `DATE` с
моментами в зоне пользователя — фиксируется в B3). Задачи без срока в период пока не попадают, см. открытый вопрос Q2 в плане.

### `get_event_by_query`

| Аргумент | Тип | Обязателен | Правило |
| --- | --- | --- | --- |
| `query` | string 1–200 | да | слова из названия/описания |
| `event_types` | список типов | нет | |
| `start_time`, `end_time` | ISO 8601 | нет | сужают поиск, полуинтервал |
| `limit` | int 1–50 | нет | по умолчанию 10 |

Результат: `{"events": [Event, ...]}` — по релевантности, затем по времени. Ничего не найдено: `{"events": []}`.

### Для роли 3: отличия от описанной в `docs/ML*.md` заглушки

- Добавлены `description` и `end_time` (встреча). Длительность встречи не придумывать.
- Формат ошибки — объект `{"error": {"code", "message", "field"}}`, а не строка. `ToolCall.ok` (`"error" not in result`) остаётся корректным.
- Нет `already_exists`: повтор того же действия возвращает `replayed: true`.
- `delete_event` не входит в MVP.
- У встреч и напоминаний `status = null` — совпадает с заглушкой.

## 4. REST для Mini App (роль 5)

Авторизация: каждый запрос несёт исходную строку `initData` из MAX Bridge. Сервер проверяет подпись и
свежесть по документации MAX; `initDataUnsafe` не используется. Имя заголовка — предложение
`Authorization: tma <initData>`, финально фиксируется по документации MAX (Q5 в плане).

| Метод и путь | Параметры | Ответ |
| --- | --- | --- |
| `GET /api/events` | `start_time`, `end_time`, `event_types` (повторяемый) | `200 {"events": [...], "truncated": bool}` |
| `GET /api/events/search` | `query`, `event_types`, `start_time`, `end_time`, `limit` | `200 {"events": [...]}` |
| `PATCH /api/tasks/{id}/status` | тело `{"status": "todo" \| "in_progress" \| "done"}` | `200 Event` |

Коды: `401` — нет/неверная/просроченная initData; `404` — запись не найдена у пользователя (в том числе чужая);
`422` — `validation_error` (тело — тот же формат `{"error": {...}}`); `503` — `temporary_unavailable`.
Бот и Mini App читают одну БД: данные одинаковые, локальное время показывает клиент по `timezone` пользователя.

## Журнал версий

| Версия | Дата | Изменение |
| --- | --- | --- |
| v0 | 2026-10-23 | Первое предложение контракта: три инструмента, DTO, формат ошибок, REST Mini App |
