# Интеграция с ролью 2: MAX bot

> **Статус: v1 — основной сценарий бота реализован** (`app/bot/`, локальный запуск `python -m app.bot`).
> Потребитель потока уведомлений (раздел 6) ещё не реализован. Изменения — только через журнал версий.

Документ для разработчиков и их агентов: ролей 1 (агент, worker, dispatcher, сборка API), 3 (ASR),
4 (MCP, REST, initData), 5 (Mini App), 6 (метрики). Внутренние правила роли 2 — в [`AGENTS.md`](AGENTS.md).

Сверено с документацией MAX на 2026-10-25: [`POST /subscriptions`](https://dev.max.ru/docs-api/methods/POST/subscriptions),
[`POST /messages`](https://dev.max.ru/docs-api/methods/POST/messages), объекты
[`Update`](https://dev.max.ru/docs-api/objects/Update), [`Message`](https://dev.max.ru/docs-api/objects/Message),
[`User`](https://dev.max.ru/docs-api/objects/User). Что не удалось подтвердить, перечислено в разделе 10.

## 0. Что сделать каждой роли

| Роль | Что нужно для интеграции | Раздел |
| --- | --- | --- |
| 1 | Реализовать `Backend.answer` поверх agent runner; смонтировать `webhook_router` в API; согласовать ключ `bot:busy` с состояниями диалога; заменить `BackgroundRunner` на jobs/outbox | 3, 4, 5, 10 |
| 3 | Ничего нового: бот вызывает `SpeechRecognizer.transcribe(audio, suffix)` и обрабатывает его ошибки по `docs/ML.md` §1 | 4 |
| 4 | Ничего нового: владелец — `str(user_id)` MAX, как в `initData`; `MAX_BOT_TOKEN` общий | 1, 7 |
| 5 | Ничего нового в этой версии | — |
| 6 | Панели по метрикам раздела 8 | 8 |

## 1. Сценарий

```text
bot_started или /start ─▶ приветствие
сообщение ─▶ дедупликация события ─▶ занято? ─да─▶ «Сейчас обрабатывается ваш предыдущий запрос…»
                                         └нет─▶ блокировка ─▶ «Принял запрос, обрабатываю…» ─▶ фоновая обработка
фоновая обработка: текст ─────────────────────────────────▶ Backend.answer ─▶ ответ пользователю
                   голос ─▶ загрузка ─▶ ASR ─▶ «Распознано: …» ─▶ Backend.answer ─▶ ответ пользователю
                   в любом исходе ─▶ снятие блокировки
```

- Один активный запрос на пользователя. Пока он обрабатывается, **любой** ввод — текст, голос, `/start`,
  `bot_started`, неподдерживаемое вложение — получает сообщение о занятости и не запускает вторую цепочку.
- Сообщение без текста и голоса (стикер, картинка) получает подсказку «Я понимаю текст и голосовые…».
- Текст без единой буквы или цифры (только смайлики, знаки препинания, пробелы) получает
  «Введите корректный запрос.» и не обрабатывается. Смайлики внутри обычного текста допустимы.
- Голос с подписью обрабатывается по аудио, подпись игнорируется.
- Владелец — `owner_id = str(sender.user_id)`, ответ уходит в `recipient.chat_id`. Сообщения ботов
  и записи каналов без отправителя игнорируются.
- Подтверждение создания формирует backend по результату инструмента; бот отправляет его текст как есть.
  Ошибка или пустой ответ backend — честное «Не получилось обработать запрос…».

## 2. Модули

| Файл | Ответственность |
| --- | --- |
| `settings.py` | `BotSettings.from_env()`: токен, адрес API, секрет webhook, зона, лимиты, таймауты |
| `updates.py` | `parse_update(raw) -> BotStarted \| MessageCreated \| Ignored`, `normalize(...) -> IncomingMessage` |
| `client.py` | `MaxClient`: `send_text`, `download_audio`, `get_updates`; `SendResult`, ошибки аудио |
| `gate.py` | `RedisGate`: блокировка пользователя и дедупликация событий |
| `service.py` | `BotService.handle(event)`, фоновая обработка, порт `Backend`, `BackgroundRunner` |
| `messages.py` | Тексты для пользователя |
| `webhook.py` | `handle_webhook(...)`, `webhook_router(bot, secret=...)` для FastAPI |
| `polling.py`, `__main__.py` | Long polling для локальной разработки без публичного HTTPS |
| `metrics.py` | Prometheus-метрики бота |

## 3. Порт `Backend` (роль 1)

```python
@dataclass(frozen=True, kw_only=True)
class BackendRequest:              # app.bot.service
    request_id: UUID
    job_id: str
    owner_id: str                  # str(user_id) MAX — то же значение для X-Owner-Id MCP
    chat_id: str
    text: str                      # текст пользователя или расшифровка голоса
    reference_time: datetime       # UTC, момент первого приёма; не меняется при повторе
    timezone: str                  # IANA, сейчас из MAX_DEFAULT_TIMEZONE


class Backend(Protocol):
    async def answer(self, request: BackendRequest) -> str: ...
```

Пример адаптера к `Assistant.handle` из `docs/ML.md` §1:

```python
class AgentBackend:
    def __init__(self, assistant: Assistant) -> None:
        self._assistant = assistant

    async def answer(self, request: BackendRequest) -> str:
        reply = await self._assistant.handle(
            request.owner_id, request.text,
            reference_time=request.reference_time, timezone=request.timezone,
        )
        return reply.text
```

- Исключение из `answer` бот превращает в «Не получилось обработать запрос…» и снимает блокировку.
- Вся обработка ограничена `BOT_PROCESSING_TIMEOUT_S` (120 с). По истечении — то же сообщение.
- `action_id` для MCP формирует роль 1 от `job_id` (предложение роли 4: `f"{job_id}:{номер вызова}"`).

## 4. Голос

- Аудио берётся из вложения `type: "audio"` (`payload.url`) или из файла `type: "file"` с аудиорасширением
  (`.ogg`, `.mp3`, `.m4a`, …) в `filename` или URL; неизвестное расширение передаётся пустым, ffmpeg определяет формат сам.
- **Ограничение MAX.** Для нативных голосовых из части версий приложения MAX присылает `message_created`
  только с `timestamp`, `user_locale`, `update_type` — без `message`, отправителя и чата
  ([max-bot-api-client-ts#250](https://github.com/max-messenger/max-bot-api-client-ts/issues/250)).
  Ответить на такое событие невозможно: бот пишет в лог `message_without_payload` и метрику `outcome="no_payload"`.
- Загрузка: только HTTPS, хост из `MAX_AUDIO_HOSTS` (по суффиксу), без IP-адресов и учётных данных в URL,
  до трёх редиректов с повторной проверкой хоста, лимит `MAX_AUDIO_MAX_BYTES` (10 МБ), без токена бота.
- Распознавание — `SpeechRecognizer` роли 3. `python -m app.bot` проверяет его при старте: `asr_ready`
  или `asr_unavailable` с `reason` (`ffmpeg_not_found`, `gigaam_not_installed`, `configuration_error`).
- Если наш ASR не настроен или вернул `AsrError` либо у вложения нет URL, используется расшифровка MAX
  (`transcription` вложения), если она есть. Лимиты размера и длительности она не обходит.
  Без неё голос получает вежливый отказ, текст продолжает работать.

| Ошибка | Сообщение пользователю | Backend вызывается |
| --- | --- | --- |
| `AudioTooLargeError` | «Файл записи слишком большой…» | нет |
| `AudioSourceError`, `AudioDecodeError` | «Не удалось прочитать запись…» | нет |
| `AudioFetchError`, `AsrError` | «Не удалось распознать голосовое…» | нет |
| `AudioTooLongError` | «Голосовое слишком длинное… 25 секунд» | нет |
| пустая расшифровка | «Не расслышал, повторите, пожалуйста.» | нет |

## 5. Webhook и запуск

```python
from app.bot.webhook import webhook_router

app.include_router(webhook_router(bot, secret=bot_settings.webhook_secret))  # POST /max/webhook
```

| Ситуация | HTTP для MAX |
| --- | --- |
| Принято, повтор события, занято, приветствие | 200 |
| Неподдерживаемый тип события | 200 (`ignored`) |
| Нет или неверный `X-Max-Bot-Api-Secret` | 401, до разбора тела |
| Тело не JSON или не по схеме, больше 1 МБ | 200 (`invalid`, метрика и лог): секрет подтверждает MAX, повтор не поможет, а не-200 в течение 8 ч отключает подписку |
| Redis недоступен | 500: MAX повторит доставку, отметка события снимается |

- Webhook не ждёт ASR и backend: он только проверяет, ставит блокировку, отправляет статус
  и передаёт работу в `submit`. MAX требует HTTP 200 за 30 секунд, порт 443 и сертификат доверенного УЦ.
- **Ограничение текущей версии.** `BackgroundRunner` выполняет работу в том же процессе и не долговечен:
  при падении процесса запрос теряется, блокировка снимается по TTL. Целевой путь (корневой §10,
  `docs/ARCHITECTURE.md`): `submit` заменяется приёмом задания роли 1 (сообщение, job и outbox
  в PostgreSQL), а фоновую часть `BotService._process` выполняет worker. Сигнатура `Submit` не меняется.
- Локальный запуск без HTTPS — long polling `GET /updates`:

```bash
docker compose up -d --wait redis
MAX_BOT_TOKEN=<токен тестового бота> uv run --locked --env-file .env python -m app.bot
```

  В `python -m app.bot` подключён `DemoBackend`: он отвечает «Демо-режим: обработчик запросов ещё не
  подключён…» и **не создаёт записей**. Это стенд для проверки сценария бота, не интеграция с агентом.
  `BOT_DEMO_DELAY_S` (по умолчанию 0) задерживает демо-ответ, чтобы вручную проверить сообщение о занятости.
  Отклонённый домен аудио пишется в лог событием `audio_source_rejected` (только домен, без URL).
  Webhook и long polling одновременно не используются: при активной подписке отключите её перед polling.
- **Локальный webhook-режим** (нужен для нативных голосовых: при long polling MAX присылает их пустыми,
  через webhook — с аудио, по [#250](https://github.com/max-messenger/max-bot-api-client-ts/issues/250)):

```bash
# 1. MAX_WEBHOOK_SECRET=<5–256 символов [A-Za-z0-9_-]> в .env
# 2. HTTPS-туннель на локальный порт, например: cloudflared tunnel --url http://localhost:8080
uv run --locked --env-file .env python -m app.bot webhook            # 127.0.0.1:8080, POST /max/webhook, GET /health
uv run --locked --env-file .env python -m app.bot subscribe --url https://<туннель>/max/webhook
uv run --locked --env-file .env python -m app.bot subscriptions      # список подписанных URL
uv run --locked --env-file .env python -m app.bot unsubscribe --url https://<туннель>/max/webhook
```

  Это стенд разработки на `build_webhook_app` (тот же `webhook_router` и `BotService`), не целевой API:
  в рабочей системе маршрут монтирует роль 1. Подписка: `POST /subscriptions` с `url`,
  `update_types: ["bot_started", "message_created"]`, `secret`; удаление — `DELETE /subscriptions?url=`.
  MAX требует порт 443 без порта в URL и доверенный сертификат. Ctrl+C/SIGTERM останавливает приём,
  ждёт активные запросы до 10 с и снимает блокировки. Сервер uvicorn приходит транзитивно (через fastmcp).

## 6. Потребитель уведомлений

Не реализован в v1. План без изменений: `docs/notification-stream.md`, поток `it-gorky:v1:notifications:max`,
группа `max-delivery-v1`, ACK через `NotificationStream.report`, `BLOCK` 1000 мс, `XAUTOCLAIM` 60 с,
бюджет 3 получения, таймаут отправки 20 с. Исходы `SendResult.outcome` уже совпадают с исходами контракта.

## 7. Ключи Redis и настройки

| Ключ | Тип | Владелец | TTL | Назначение |
| --- | --- | --- | --- | --- |
| `it-gorky:v1:bot:busy:<owner_id hex>` | string: `job_id` | роль 2 | `BOT_BUSY_TTL_S` (180 с) | один активный запрос; снимает только владелец `job_id` (Lua compare-and-delete) |
| `it-gorky:v1:bot:event:<event_id hex>` | string `"1"` | роль 2 | 9 ч | дедупликация повторной доставки (MAX повторяет до 8 ч) |
| `it-gorky:v1:notifications:*` | stream | роль 1 / роль 2 | нет | раздел 6 |
| `assistant:history:<user_id>` | list | роль 3 / роль 1 | 7 дней | история диалога, ботом не используется |

`event_id`: `max:message:<mid>` для сообщений, `max:bot_started:<chat_id>:<timestamp>` для старта.
Окончательная дедупликация в целевой архитектуре — в приёме задания (PostgreSQL); ключ бота её не заменяет.
Соединение — общий `redis_connection(RedisSettings.from_env())`, переменные `REDIS_*`.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `MAX_BOT_TOKEN` | — | токен бота; заголовок `Authorization` без `Bearer`; общий с проверкой `initData` |
| `MAX_API_URL` | `https://platform-api2.max.ru` | адрес Bot API |
| `MAX_CA_BUNDLE_FILE` | пусто | дополнительный PEM-сертификат к публичным УЦ (сертификат Минцифры, если MAX его использует) |
| `MAX_WEBHOOK_SECRET` | пусто | 5–256 символов `[A-Za-z0-9_-]`; обязателен для webhook |
| `MAX_DEFAULT_TIMEZONE` | `Europe/Moscow` | явная настройка продукта, пока зона не хранится в профиле |
| `MAX_AUDIO_HOSTS` | `max.ru` | суффиксы хостов для загрузки аудио, через запятую |
| `MAX_AUDIO_MAX_BYTES` | `10485760` | лимит размера аудио |
| `MAX_SEND_TIMEOUT_S` | `10` | таймаут одного HTTP-вызова MAX, ≤ 20 |
| `BOT_PROCESSING_TIMEOUT_S` | `120` | предел ASR + backend на один запрос |
| `BOT_BUSY_TTL_S` | `180` | TTL блокировки; должен превышать обработку и все попытки отправки |
| `BOT_MAX_CONCURRENCY` | `8` | число одновременно обрабатываемых запросов в процессе |

Переменные с безопасными значениями по умолчанию внесены в `.env.example` (блок «Role 2»).

## 8. Метрики (роль 6)

Метки — только перечисления.

| Метрика | Тип | Метки |
| --- | --- | --- |
| `assistant_bot_updates_total` | counter | `kind` (`text`, `audio`, `start`, `other`), `outcome` (`accepted`, `busy`, `welcomed`, `unsupported`, `invalid_input`, `duplicate`, `ignored`, `no_payload`, `rejected`, `unauthorized`, `invalid`, `error`) |
| `assistant_bot_send_total` | counter | `purpose` (`welcome`, `status`, `transcript`, `reply`, `busy`, `error`), `outcome` (`delivered`, `retryable_failure`, `permanent_failure`, `unknown`) |
| `assistant_bot_send_seconds` | histogram | `purpose` |
| `assistant_bot_audio_fetch_total` | counter | `outcome` (`ok`, `source_error`, `too_large`, `fetch_error`) |
| `assistant_bot_processing_total` | counter | `outcome` (`replied`, `not_heard`, `audio_error`, `asr_error`, `timeout`, `failed`, `cancelled`) |

Пример: доля занятых ответов — `sum(rate(assistant_bot_updates_total{outcome="busy"}[5m])) / sum(rate(assistant_bot_updates_total[5m]))`.

## 9. Отправка сообщений

- `POST /messages?chat_id=<id>`, тело `{"text": ...}`, до 4000 символов; длинный ответ делится по строкам/словам.
- 429, 5xx и сетевые ошибки — до 3 попыток с задержкой 1 и 2 с (лимит MAX — 2 сообщения в секунду на диалог).
  Остальные 4xx не повторяются. Таймаут — исход `unknown` без повтора: запрос мог дойти, повтор дал бы дубль.
- Неудачная отправка статуса не останавливает обработку; итог отражается в метриках и логах без текста.

## 10. Не подтверждено документацией и открытые вопросы

| Вопрос | Что сделано / с кем |
| --- | --- |
| Поля `Message.body` (`mid`, `text`, `attachments`), `Recipient.chat_id`, `payload.url` аудио: страницы объектов не открылись | Текст подтверждён ручной проверкой. При расхождении событие не обрабатывается, причина — в логе `polling_invalid_update` / `webhook_invalid` (`reason`) |
| Нативные голосовые приходят без `message` | Ограничение MAX, исправить на нашей стороне нельзя. Обходные пути: аудиофайл вместо голосового, другая версия приложения MAX |
| Хосты, с которых MAX отдаёт аудио | По умолчанию `max.ru`; если реальный URL на другом домене, загрузка даст «Не удалось прочитать запись» — домен добавляется в `MAX_AUDIO_HOSTS` после проверки |
| Повторная выдача пакета `GET /updates` при прежнем `marker` | Polling подтверждает пакет только после обработки; повтор безопасен благодаря дедупликации |
| Ключ `bot:busy` и состояния `processing` / `awaiting_clarification` роли 1 | Блокировка бота снимается после ответа, поэтому ответ на уточнение принимается. Объединить с ключами роли 1 при появлении worker |
| Где хранится `timezone` пользователя | Сейчас `MAX_DEFAULT_TIMEZONE`; нужен профиль — роль 1 |
| `BackgroundRunner` → jobs/outbox | Роль 1; см. раздел 5 |
| Групповые чаты | Блокировка по пользователю, ответ в чат сообщения; ключ истории с `chat_id` — роли 1, 3 |

## Журнал версий

| Версия | Дата | Изменение |
| --- | --- | --- |
| v0 | 2026-10-25 | Первое предложение: `IncomingMessage`, webhook → `admit`, `load_audio`/`send_text`/`edit_text`, потребитель уведомлений, ключи Redis, настройки, метрики |
| v1 | 2026-10-25 | Реализован сценарий бота: приветствие, статус, текст → `Backend`, голос → ASR → `Backend`, сообщение о занятости, ответ. Порт `admit` заменён на `Backend` + `Submit`; `load_audio` — `MaxClient.download_audio`; `edit_text` отложен; ключи `bot:busy`, `bot:event`; адрес `platform-api2.max.ru`, заголовок `X-Max-Bot-Api-Secret`; long polling для локальной разработки |
| v1.1 | 2026-10-25 | Без изменения контракта: переменные бота в `.env.example`, `app/bot` в проверке mypy; `BOT_DEMO_DELAY_S` и лог отклонённого домена аудио; запуск на Windows |
| v1.2 | 2026-10-26 | Без изменения контракта: причина в логе `polling_failed` (`error_code`: `http_401`, `tls_certificate`, `connect_error`, `proxy_error`, `timeout`…), `MAX_CA_BUNDLE_FILE` |
| v1.3 | 2026-10-26 | Без изменения контракта `Backend`: только смайлики → «Введите корректный запрос.»; аудиофайлы `file`; расшифровка MAX как запасной вариант; пустые `message_created` логируются; причина невалидного события в логе; проверка ASR при старте |
| v1.4 | 2026-10-26 | Локальный webhook-режим `python -m app.bot webhook`, команды `subscribe` / `unsubscribe` / `subscriptions`, проверка формата `MAX_WEBHOOK_SECRET`, корректная остановка по сигналу в обоих режимах |
