# Интеграция с ролью 2: MAX bot

> **Статус: v2.12 — одно подтверждение голосового изменения или удаления** (`app/bot/`, локальный запуск `python -m app.bot`).
> Потребитель потока уведомлений реализован (раздел 6); публикацию напоминаний пока делает
> временный планировщик бота до появления dispatcher роли 1. Изменения — только через журнал версий.

Документ для разработчиков и их агентов: ролей 1 (агент, worker, dispatcher, сборка API), 3 (ASR),
4 (MCP, REST, initData), 5 (Mini App), 6 (метрики). Внутренние правила роли 2 — в [`AGENTS.md`](AGENTS.md).

Изменение v2.2 (2026-10-26): `Sender` дополнен
`async edit_text(*, message_id: str, text: str) -> SendResult`.
Роль 1 при подстановке отправителя реализует оба метода; роль 6 учитывает `purpose="edit"`
в существующих send-метриках. Другие роли не меняют входной контракт.
BotService сохраняет ID сообщения «Принял запрос…» в текущем задании и заменяет его
ответом, уточнением или ошибкой. При отказе MAX/отсутствующем ID/ответе длиннее 4000
символов используется обычная отправка (длинный ответ разбивается); backend повторно
не запускается. Неопределённый результат редактирования с последующей отправкой может
дать два одинаковых текста. После аварии статус может остаться: runner пока в памяти.

Клиент использует [официальный PUT /messages](https://dev.max.ru/docs-api/methods/PUT/messages),
`message_id` в query, `text` в JSON, успех только при `success: true`.
Повтор PUT с тем же ID и текстом ограничен SEND_ATTEMPTS и безопасен для операции
замены. Обычная отправка с неопределённым сетевым исходом по-прежнему не ретраится.
Статус редактируется один раз за запрос; при 429 применяется существующий backoff.

Сверено с документацией MAX на 2026-10-25: [`POST /subscriptions`](https://dev.max.ru/docs-api/methods/POST/subscriptions),
[`POST /messages`](https://dev.max.ru/docs-api/methods/POST/messages), объекты
[`Update`](https://dev.max.ru/docs-api/objects/Update), [`Message`](https://dev.max.ru/docs-api/objects/Message),
[`User`](https://dev.max.ru/docs-api/objects/User). Что не удалось подтвердить, перечислено в разделе 10.

## 0. Что сделать каждой роли

| Роль | Что нужно для интеграции | Раздел |
| --- | --- | --- |
| 1 | `AgentBackend` подключён; дальше смонтировать `webhook_router` в общий API, согласовать состояния диалога и заменить `BackgroundRunner` на jobs/outbox. Dispatcher публикует наступившие напоминания через `NotificationStream.publish` — после этого временный `app/bot/reminders.py` удаляется | 3, 4, 5, 6, 10 |
| 3 | Ничего нового: бот вызывает `SpeechRecognizer.transcribe(audio, suffix)` и обрабатывает его ошибки по `docs/ML.md` §1 | 4 |
| 4 | Ничего нового: владелец — `str(user_id)` MAX, как в `initData`; `MAX_BOT_TOKEN` общий. Временный планировщик только читает `events` (напоминания, `deleted_at IS NULL`); переименование колонок `scheduled_at`, `event_type`, `deleted_at` согласовать с ролью 2 | 1, 6, 7 |
| 5 | Mini App открывается кнопкой бота (v2.9): `initData` брать из `window.WebApp.initData` (MAX Bridge), API — относительный `/api` на том же домене | 1, 11 |
| 6 | Панели по метрикам раздела 8, включая доставку уведомлений (v2.7) | 8 |

## 1. Сценарий

v2.12 уточняет путь голосового изменения/удаления для всех трёх типов записей:
ASR → агент готовит конкретный план без записи → расшифровка и план с кнопками →
«Да, верно» исполняет сохранённый план, без второго подтверждения и нового вызова LLM.
Если выбор неоднозначен, сначала приходит уточнение без кнопок. Создание по-прежнему
показывает подтверждение расшифровки перед вызовом агента; чтение выполняется сразу.

Поля внутреннего контракта: `AgentReply.confirmation_operation_id` и
`Reply.confirmation_operation_id` обозначают ход с сохранённым планом;
`BackendRequest.confirmation_operation_id` и `IncomingMessage.confirmation_operation_id`
передаются только из сохранённого callback. MAX и модель эти значения не устанавливают.
В `bot:voice` поле `text` равно «да» для плана, а `confirmation_operation_id` связывает
кнопку с подготовившим его заданием. Старые записи без поля читаются как подтверждение
обычной расшифровки. Владелец, чат, срок 15 минут и действие проверяются на сервере.
«Нет» отменяет также агентский план. Новый запрос делает старую кнопку неактуальной.

Для явного удаления нескольких выбранных записей и одинакового переименования
поддерживается общий план до пяти записей из полной выдачи. При принятии каждая
операция сохраняет свой action_id; повтор не создаёт новое действие. Это несколько
отдельных транзакций, поэтому возможен частичный результат при сбое инфраструктуры.
Разные изменения времени нескольких записей и смешанное создание с удалением требуют
отдельных запросов. Подтверждённые tool call/result сохраняются последовательно,
что совместимо с ограничением GigaChat на один function_call в сообщении.

```text
bot_started или /start ─▶ приветствие
сообщение ─▶ дедупликация события ─▶ занято? ─да─▶ «Сейчас обрабатывается ваш предыдущий запрос…»
                                         └нет─▶ блокировка ─▶ «Принял запрос, обрабатываю…» ─▶ фоновая обработка
фоновая обработка: текст ─────────────────────────────────▶ Backend.answer ─▶ ответ пользователю
                   голос ─▶ загрузка ─▶ ASR ─▶ чтение/поиск ─▶ Backend.answer
                                           ├▶ создание ─▶ «Распознано: … Все верно?» [Да, верно] [Нет]
                                           └▶ изменение/удаление ─▶ backend: план ─▶ расшифровка + план + кнопки
                   в любом исходе ─▶ снятие блокировки
«Да, верно» ─▶ удалить сообщение с кнопками ─▶ блокировка ─▶ «Принял запрос…» ─▶ Backend.answer (сохранённый план либо создание) ─▶ ответ
«Нет» ─────▶ «Повторите запрос.»
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
- Ответ backend может быть `Reply(text, buttons)`: кнопки-callback в одну строку под последним сообщением.
  Кнопки «Да, все верно» (`confirm:yes`, пока без действия, нажатие только подтверждается через
  `POST /answers`) и «Нет» (`confirm:no` → «Повторите запрос.», следующее сообщение — обычный новый запрос)
  готовы в `CONFIRM_BUTTONS`. Демо-backend удалён (v2), `AgentBackend` сейчас возвращает текст без кнопок.
- Обычный ответ или ошибка заменяют сообщение «Принял запрос, обрабатываю…» (`edit_text`, см. v2.2).
  Ответ с кнопками приходит новым сообщением, а статус удаляется (`DELETE /messages`).
  Голос (v2.11): просмотр, поиск и вопросы о задачах, встречах и напоминаниях сразу
  передаются в backend с исходным `reference_time`, без кнопок подтверждения расшифровки.
  Для создания, изменения и удаления всех трёх типов, а также неоднозначных коротких
  уточнений, распознанный текст сначала показывается как «Распознано: <текст>
Все верно?» с кнопками
  «Да, верно» (`voice:yes:<id>`) и «Нет» (`voice:no:<id>`); статус «Принял запрос…» удаляется.
  Текст хранится в `bot:voice` (раздел 7) до ответа, не дольше 15 минут. «Да, верно» удаляет сообщение
  с кнопками и отправляет текст в `Backend.answer` с `reference_time` момента прихода голосового;
  «Нет» отбрасывает текст и отвечает «Повторите запрос.». Кнопки старого голосового после нового,
  после «Нет» или по истечении срока отвечают «Этот запрос уже неактуален…» и backend не вызывают.
  Во время обработки другого запроса «Да, верно» получает сообщение о занятости, текст сохраняется.
  Ошибка или пустой ответ backend — честное «Не получилось обработать запрос…».

- Посторонние вопросы в тексте или расшифровке получают фиксированную подсказку
  о назначении бота без вызова backend и без подтверждения голоса. Дополнительный
  фильтр агента покрывает прямой текстовый вход; промпт ограничивает область ответа.
  Короткие ответы на уточнение сохраняются, название задачи про Python допустимо.
  Общие правила классификации находятся в `app/agent/permissions.py`; они не дают
  разрешение на запись: проверка полномочий каждого инструмента сохраняется.

- Mini App (v2.9). При заданном `MAX_MINI_APP_BOT` приветствие (`bot_started`, `/start`, `/help`) несёт кнопку
  «Открыть приложение» (`type: "open_app"`, `web_app` — публичное имя бота), а команда `/app` присылает её
  отдельным сообщением. Без настройки кнопки нет, `/app` отвечает «Приложение пока недоступно…».
  `/app` во время обработки получает сообщение о занятости. Адрес приложения задаётся не ботом, а в
  настройках бота на платформе MAX для партнёров; там же MAX добавляет свою кнопку запуска в чат.

## 2. Модули

| Файл | Ответственность |
| --- | --- |
| `settings.py` | `BotSettings.from_env()`: токен, адрес API, секрет webhook, зона, лимиты, таймауты |
| `updates.py` | `parse_update(raw) -> BotStarted \| MessageCreated \| Ignored`, `normalize(...) -> IncomingMessage` |
| `client.py` | `MaxClient`: `send_text`, `send_to_user`, `edit_text`, `download_audio`, `get_updates`; `SendResult`, ошибки аудио |
| `gate.py` | `RedisGate`: блокировка пользователя и дедупликация событий |
| `service.py` | `BotService.handle(event)`, фоновая обработка, порт `Backend`, `BackgroundRunner` |
| `messages.py` | Тексты для пользователя |
| `webhook.py` | `handle_webhook(...)`, `webhook_router(bot, secret=...)` для FastAPI |
| `polling.py`, `__main__.py` | Long polling для локальной разработки без публичного HTTPS |
| `metrics.py` | Prometheus-метрики бота |
| `delivery.py` | `DeliveryConsumer`: поток уведомлений → `POST /messages?user_id=` по контракту `docs/notification-stream.md` |
| `reminders.py` | **Временный** `ReminderScheduler`: наступившие напоминания из PostgreSQL → поток уведомлений |

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

Реализован `app.agent.bot_backend.AgentBackend`. Он передаёт owner_id, chat_id,
text, reference_time и timezone без подмены. `request_id` используется для логов,
`job_id` передаётся как operation_id хода и сохраняется при повторном `answer(request)`.
Action_id отдельных MCP-вызовов строит агент из сохранённого плана этого хода.

```python
from app.agent.bot_backend import AgentBackend
backend = AgentBackend(assistant)
reply_text = await backend.answer(request)
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

  В `python -m app.bot` подключён настоящий `AgentBackend`: модель и MCP/PostgreSQL должны быть
  настроены. Зависимости собираются один раз, Redis-клиент общий для бота и истории.
  Для первого текста: `python -m app.bot polling --text-only`, без загрузки ASR.
  `MAX_TEST_CHAT_ID` при заданном значении ограничивает обработку этим чатом: остальные
  события игнорируются до дедупликации, блокировок и отправки любых статусов.
  Отклонённый домен аудио пишется в лог событием `audio_source_rejected` (только домен, без URL).
  Webhook и long polling одновременно не используются: при активной подписке отключите её перед polling.
  В обоих режимах вместе с ботом работают доставка уведомлений и временный планировщик напоминаний
  (раздел 6); им нужны Redis и PostgreSQL с миграциями. `--no-reminders` их отключает.
- **Локальный webhook-режим** (нужен для нативных голосовых: при long polling MAX присылает их пустыми,
  через webhook — с аудио, по [#250](https://github.com/max-messenger/max-bot-api-client-ts/issues/250)):

```bash
# 1. MAX_WEBHOOK_SECRET=<5–256 символов [A-Za-z0-9_-]> в .env
# 2. HTTPS-туннель на локальный порт: cloudflared tunnel --url http://127.0.0.1:8080
#    (не localhost: на Windows он разрешается в ::1, и туннель не достучится до сервера)
uv run --locked --env-file .env python -m app.bot webhook            # 127.0.0.1:8080, POST /max/webhook, GET /health
uv run --locked --env-file .env python -m app.bot subscribe --url https://<туннель>/max/webhook
uv run --locked --env-file .env python -m app.bot subscriptions      # список подписанных URL
uv run --locked --env-file .env python -m app.bot doctor             # проверка цепочки по шагам: секрет, Redis, токен, подписка, сервер, туннель, пробное событие
uv run --locked --env-file .env python -m app.bot unsubscribe --url https://<туннель>/max/webhook
```

  Это стенд разработки на `build_webhook_app` (тот же `webhook_router` и `BotService`), не целевой API:
  в рабочей системе маршрут монтирует роль 1. Подписка: `POST /subscriptions` с `url`,
  `update_types: ["bot_started", "message_created", "message_callback"]`, `secret`;
  удаление — `DELETE /subscriptions?url=`. После добавления нового типа события нужна переподписка.
  MAX требует порт 443 без порта в URL и доверенный сертификат. Ctrl+C/SIGTERM останавливает приём,
  ждёт активные запросы до 10 с и снимает блокировки. Сервер uvicorn приходит транзитивно (через fastmcp).

## 6. Напоминания и потребитель уведомлений

```text
PostgreSQL events (reminder, scheduled_at наступил) ─▶ ReminderScheduler (временно, раз в 5 с)
   ─▶ NotificationStream.publish ─▶ it-gorky:v1:notifications:max
   ─▶ DeliveryConsumer (группа max-delivery-v1) ─▶ POST /messages?user_id=<owner_id> ─▶ report + ACK
```

**Потребитель** (`delivery.py`) реализует `docs/notification-stream.md` без изменения контракта:
одна группа `max-delivery-v1`, уникальное имя consumer на запуск (`bot-<хост>-<uuid>`), `XREADGROUP >`
с `BLOCK` 1000 мс, `XAUTOCLAIM` с `min-idle-time` 60 000 мс и сохранённым курсором раз в 30 с.
Результат и ACK — только `NotificationStream.report`. Бюджет — 3 получения; при `delivery_count >= 4`
отчёт `not_attempted` / `attempts_exhausted` без отправки. Неверный envelope — `invalid_envelope`.
Получатель — `owner_id`: для пользователей бота это MAX `user_id`, поэтому отправка идёт по
`POST /messages?user_id=` без отдельной таблицы соответствия. `owner_id` не из цифр —
`permanent_failure` / `unknown_recipient`. Отправка ограничена 20 с; превышение — `unknown` /
`send_timeout` (сообщение могло дойти, повтор возможен). `StaleDeliveryError` — запись перехвачена,
обработка прекращается без отчёта. Удалённые payload в ответе `XAUTOCLAIM` — событие
`delivery_stopped` и остановка потребителя (бот продолжает отвечать). Ошибки Redis — пауза 1 с и повтор.

**Временный планировщик** (`reminders.py`, согласован командой до появления dispatcher роли 1)
раз в 5 с читает из `events` напоминания с `deleted_at IS NULL` и `now − 1 ч < scheduled_at ≤ now`
страницами по 100 (курсор `(scheduled_at, id)`), только чтение. Публикация однократна: ключ
`bot:reminder` ставится `SET NX` до `publish` и снимается, если публикация не удалась.
Перенесённое напоминание срабатывает в новое время, удалённое — не срабатывает. Пропущенные
больше часа назад (бот был выключен) не отправляются. Текст: «🔔 Напоминание: <название>»
и описание с новой строки. `notification_id` — UUIDv5 от `id` и времени напоминания.
При недоступном PostgreSQL — событие `reminders_postgres_unavailable` и повтор через 10 с.

Ограничения: публикация и ключ не атомарны (сбой между ними теряет одно напоминание, а не дублирует);
доставка at-least-once по контракту (сбой после отправки до ACK может дать дубль).
Когда dispatcher роли 1 начнёт публиковать напоминания, `reminders.py` и его запуск удаляются,
потребитель остаётся без изменений.

## 7. Ключи Redis и настройки

| Ключ | Тип | Владелец | TTL | Назначение |
| --- | --- | --- | --- | --- |
| `it-gorky:v1:bot:busy:<owner_id hex>` | string: `job_id` | роль 2 | `BOT_BUSY_TTL_S` (180 с) | один активный запрос; снимает только владелец `job_id` (Lua compare-and-delete) |
| `it-gorky:v1:bot:event:<event_id hex>` | string `"1"` | роль 2 | 9 ч | дедупликация повторной доставки (MAX повторяет до 8 ч) |
| `it-gorky:v1:bot:voice:<owner_id hex>` | string: JSON (`id`, `request_id`, `source_event_id`, `chat_id`, `received_at`, `timezone`, `text`) | роль 2 | 15 мин | распознанный голос ждёт «Да, верно» / «Нет»; один на пользователя, новый заменяет старый; забирается Lua-скриптом только при совпадении `id` (v2.10) |
| `it-gorky:v1:bot:reminder:<reminder_id hex>:<unix time>` | string `"1"` (`"invalid"` для пропущенной записи) | роль 2, временно | 25 ч (окно 1 ч + сутки) | напоминание уже опубликовано; пишет и читает только временный планировщик |
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
| `MAX_AUDIO_HOSTS` | `max.ru,oneme.ru` | суффиксы хостов для загрузки аудио, через запятую |
| `MAX_AUDIO_MAX_BYTES` | `10485760` | лимит размера аудио |
| `MAX_SEND_TIMEOUT_S` | `10` | таймаут одного HTTP-вызова MAX, ≤ 20 |
| `BOT_PROCESSING_TIMEOUT_S` | `120` | предел ASR + backend на один запрос |
| `BOT_BUSY_TTL_S` | `180` | TTL блокировки; должен превышать обработку и все попытки отправки |
| `BOT_MAX_CONCURRENCY` | `8` | число одновременно обрабатываемых запросов в процессе |
| `MAX_TEST_CHAT_ID` | пусто | необязательное ограничение локального запуска одним тестовым чатом |
| `MAX_MINI_APP_BOT` | пусто | публичное имя бота для кнопки `open_app` (v2.9); пусто — кнопки нет |

Переменные с безопасными значениями по умолчанию внесены в `.env.example` (блок «Role 2»).

## 8. Метрики (роль 6)

Метки — только перечисления.

| Метрика | Тип | Метки |
| --- | --- | --- |
| `assistant_bot_updates_total` | counter | `kind` (`text`, `audio`, `start`, `other`), `outcome` (`accepted`, `busy`, `welcomed`, `unsupported`, `invalid_input`, `duplicate`, `ignored`, `no_payload`, `rejected`, `unauthorized`, `invalid`, `error`, `app_offered`, `expired`) |
| `assistant_bot_send_total` | counter | `purpose` (`welcome`, `status`, `transcript`, `reply`, `busy`, `error`, `edit`, `repeat`, `app`, `confirm`), `outcome` (`delivered`, `retryable_failure`, `permanent_failure`, `unknown`) |
| `assistant_bot_send_seconds` | histogram | `purpose` |
| `assistant_bot_audio_fetch_total` | counter | `outcome` (`ok`, `source_error`, `too_large`, `fetch_error`) |
| `assistant_bot_processing_total` | counter | `outcome` (`replied`, `awaiting_confirmation`, `not_heard`, `audio_error`, `asr_error`, `timeout`, `failed`, `cancelled`) |
| `assistant_bot_notification_deliveries_total` | counter | `status` (`delivered`, `retrying`, `dead_lettered`), `outcome` (исходы контракта) |
| `assistant_bot_reminders_published_total` | counter | — |

Пример: доля занятых ответов — `sum(rate(assistant_bot_updates_total{outcome="busy"}[5m])) / sum(rate(assistant_bot_updates_total[5m]))`.

## 9. Отправка сообщений

- `POST /messages?chat_id=<id>`, тело `{"text": ...}`, до 4000 символов; длинный ответ делится по строкам/словам.
  Уведомления без входящего сообщения — `POST /messages?user_id=<id>` (`send_to_user`), с теми же повторами.
- Отправки одному адресату (`chat_id` или `user_id`) идут не чаще одной в 0,5 с (`MIN_SEND_INTERVAL_S`),
  в том числе повторы; разные адресаты друг друга не ждут. Ограничение действует в пределах процесса.
- 429, 5xx и сетевые ошибки — до 3 попыток с задержкой 1 и 2 с (лимит MAX — 2 сообщения в секунду на диалог).
  Остальные 4xx не повторяются. Таймаут — исход `unknown` без повтора: запрос мог дойти, повтор дал бы дубль.
- Неудачная отправка статуса не останавливает обработку; итог отражается в метриках и логах без текста.

## 10. Не подтверждено документацией и открытые вопросы

| Вопрос | Что сделано / с кем |
| --- | --- |
| Поля `Message.body` (`mid`, `text`, `attachments`), `Recipient.chat_id`, `payload.url` аудио: страницы объектов не открылись | Текст подтверждён ручной проверкой. При расхождении событие не обрабатывается, причина — в логе `polling_invalid_update` / `webhook_invalid` (`reason`) |
| Нативные голосовые приходят без `message` | Только при long polling (ограничение MAX). Через webhook голосовые приходят полностью — поэтому основной режим webhook (v1.4) |
| Хосты, с которых MAX отдаёт аудио | Проверено на реальном событии: `a.oneme.ru`. По умолчанию разрешены `max.ru` и `oneme.ru`; новый домен добавляется в `MAX_AUDIO_HOSTS` после проверки (событие `audio_source_rejected`) |
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

| v2 | 2026-10-26 | Подключён AgentBackend: owner/chat/time/reference и operation_id от job_id. Точка запуска собирает настоящий агент/MCP/Redis, DemoBackend удалён. Режим --text-only, ограничение MAX_TEST_CHAT_ID, прерывание long poll и drain перед закрытием Redis; подробный запуск docs/live-bot.md. |

| v2.1 | 2026-10-26 | Polling отказывается запускаться при активном webhook (polling_blocked_by_webhook), подписку автоматически не удаляет. Startup-событие помечает backend=agent. |

| v2.2 | 2026-10-26 | `Sender.edit_text`, финальный ответ/уточнение/ошибка заменяют статус по message_id; безопасный fallback отправки, метрики purpose=edit. |
| v2.3 | 2026-10-26 | Команда `doctor` для диагностики webhook-цепочки; лог `webhook_received`; расширенные причины ошибок аудио и ASR |
| v2.4 | 2026-10-26 | Выбран GigaAM: `python -m app.bot` загружает модель в фоне при старте (`asr_warm_up_started` / `_finished` / `_failed`); запуск с `--extra asr` |
| v2.5 | 2026-10-26 | По реальному событию: голосовые MAX отдаёт с `a.oneme.ru`, домен `oneme.ru` разрешён по умолчанию |
| v2.6 | 2026-10-26 | `Backend.answer` возвращает `str` или `Reply(text, buttons)`; статус «Принял запрос…» удаляется после ответа с кнопками (обычный ответ по-прежнему заменяет статус, v2.2); сообщение «Распознано» убрано; кнопки-callback «Да, все верно» / «Нет» (`confirm:yes` / `confirm:no`), событие `message_callback` (нужна переподписка), «Нет» → «Повторите запрос.» |
| v2.7 | 2026-10-27 | Напоминания: `DeliveryConsumer` (поток `max-delivery-v1` → `POST /messages?user_id=`), `MaxClient.send_to_user`, временный `ReminderScheduler` (PostgreSQL только чтение, ключ `bot:reminder`), запуск вместе с ботом и флаг `--no-reminders`, метрики доставки. Контракт `docs/notification-stream.md` не менялся |
| v2.8 | 2026-10-27 | Без изменения контракта: `oneme.ru` снова разрешён по умолчанию (терялся при слиянии); отправки одному адресату не чаще одной в 0,5 с; тесты сбоя записи результата доставки и отсутствия текстов и секретов в логах; уточнены подписка (`message_callback`), команда туннеля и открытые вопросы |
| v2.9 | 2026-10-30 | Кнопка Mini App: `AppButton` (`open_app`) в `Buttons`/`Sender.send_text`, приветствие с кнопкой и команда `/app` при `MAX_MINI_APP_BOT`, исход `app_offered`, `purpose="app"`. Входной контракт `Backend` не менялся; реализация `Sender` роли 1 должна принимать `AppButton` в `buttons` |
| v2.10 | 2026-10-30 | Подтверждение голоса: «Распознано: … Все верно?» с кнопками `voice:yes:<id>` / `voice:no:<id>`, ключ `bot:voice` (15 мин), `CallbackPressed.message_id`, исходы `expired` и `awaiting_confirmation`, `purpose="confirm"`. Контракт `Backend` не менялся: на backend по-прежнему приходит только текст, после подтверждения |
| v2.11 | 2026-10-30 | Просмотр и поиск голосом сразу вызывают backend; создание/изменение/удаление задач, встреч и напоминаний сохраняют подтверждение расшифровки. Посторонние вопросы отсекаются до backend. Неоднозначные короткие голосовые уточнения сохраняют подтверждение. DTO и ключи Redis не менялись |
| v2.12 | 2026-10-30 | Голосовое изменение/удаление сначала готовит конкретный план без записи. Кнопка подтверждает и исполняет этот план за один шаг, без второго вопроса. Добавлен необязательный confirmation_operation_id; старые сообщения совместимы. Поддержан общий план для нескольких выбранных записей; отмена очищает план, старые и чужие кнопки не исполняются |
