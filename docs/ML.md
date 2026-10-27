# ML-часть: ASR + LLM-агент

> Актуальный backend-контракт агента, MCP client и запуск: [text-agent.md](text-agent.md).
> Примеры ниже описывают прежний прототип; сырой FastMCP Client теперь заменён
> на McpGateway с доверенными заголовками. Текущий MCP v6 имеет шесть инструментов,
> включая update_event и delete_event: [event-editing.md](event-editing.md).
> ENABLE_DELETE_EVENT теперь выбирает delete-сценарии eval, а не отключает инструмент продукта.

Владелец — роль 3 (AGENTS §5). Здесь интерфейсы, которые роль 1 подключает к очереди и боту,
порядок выбора ASR, запуск и eval.

| Где | Что | Владелец |
|---|---|---|
| `app/llm/` | промпт (`prompts.py`), фабрика GigaChat (`model.py`), очистка ответа для голоса (`speech.py`) | роль 3 |
| `app/asr/` | протокол `SpeechRecognizer`, GigaAM, Yandex SpeechKit, `factory.build_recognizer` | роль 3 |
| `app/agent/` | `Assistant.handle()`, guard, история, MCP tools → LangChain. **Готовая реализация для роли 1**: агентский цикл её, контракты меняются по согласованию | роль 1 |
| `app/config.py`, `app/metrics.py` | настройки и метрики Prometheus ML-части | роль 3, сбор метрик — роль 1 |
| `tests/fakes/` | SQLite-заглушка MCP-сервера для тестов и eval. **Не реализация роли 4** | тесты |
| `tools/demo/` | dev-демо: веб-страница с микрофоном и CLI-чат. Не продукт | роль 3 |
| `evals/` | eval агента и ASR, Cloud.ru только для сравнения моделей | роль 3 |

## 1. Контракт для роли 1 (agent runner)

### Что вызывать

| Шаг | Вызов | Результат |
|---|---|---|
| Голос → текст | `await recognizer.transcribe(audio: bytes, suffix: str)` | `Transcript` |
| Текст → действие и ответ | `await assistant.handle(user_id, text, reference_time=..., timezone=...)` | `AgentReply` |
| Сброс диалога | `await assistant.reset(user_id)` | — |

Оба вызова асинхронные и **не блокируют event loop**:
- ffmpeg и GigaAM работают в `asyncio.to_thread`. Модель GigaAM обслуживает один запрос за раз (`threading.Lock`), остальные ждут в своих потоках;
- SpeechKit, GigaChat (`langchain-gigachat` → `_agenerate`), Redis (`redis.asyncio`) и MCP client работают через async-клиенты.

### Типы

```python
@dataclass(frozen=True)
class Transcript:  # app.asr.base
    text: str  # "" — речь не распознана
    latency_s: float  # декодирование + распознавание
    audio_s: float  # длительность аудио, единица стоимости ASR


@dataclass
class AgentReply:  # app.agent.agent
    text: str  # готов к отправке: без markdown, одна строка
    tool_calls: list[ToolCall]  # вызовы инструментов этого хода
    input_tokens: int
    output_tokens: int
    cost_rub: float  # по LLM_PRICE_*; 0 на пакете GigaChat
    latency_s: float
    retried: bool  # guard поймал ложное «записал» и сделал повтор


@dataclass(frozen=True)
class ToolCall:
    name: str  # create_event | get_events_by_period | get_event_by_query
    args: dict  # аргументы модели, без user_id
    result: dict | None  # JSON-ответ MCP, напр. {"created": {...}} или {"error": "..."}
    ok: bool  # property: ответ есть и в нём нет "error"
```

### Ошибки

| Исключение | Когда | Что делать роли 1 |
|---|---|---|
| `AudioTooLongError` | аудио длиннее 25 с (`MAX_DURATION_S`) | ответить пользователю, не ретраить |
| `AudioDecodeError` | ffmpeg не прочитал файл | ответить «не удалось прочитать запись», не ретраить |
| `AsrError` | сбой провайдера (сеть, HTTP). Адаптер уже сделал 1 повтор на 429/5xx | повторить этап ASR по политике job; текст сообщения безопасен для логов |
| любое из `handle()` | LLM/MCP недоступны после повторов (`LLM_TIMEOUT_S=12`, `LLM_MAX_RETRIES=1`) | повторить job. Запись не создаётся дважды: идемпотентность по `action_id` на стороне MCP (роль 4) |

Внутри `handle()` ошибки не глотаются. Зацикливание инструментов (больше 10 шагов графа) и ложное «записал» превращаются в честный ответ (`AgentReply.text`), а не в исключение.

### Пример: обработка одного job

```python
from fastmcp import Client

from app.agent.agent import Assistant, Pricing
from app.agent.history import RedisChatHistory
from app.asr.audio import AudioDecodeError
from app.asr.base import AsrError, AudioTooLongError
from app.asr.factory import build_recognizer
from app.config import get_settings
from app.llm.model import build_chat_model
from app.storage.redis import RedisSettings, redis_connection

settings = get_settings()
recognizer = build_recognizer(settings)  # один на процесс worker

async with (
    redis_connection(RedisSettings.from_env()) as redis,  # общие REDIS_* роли 1
    Client(settings.mcp_url) as mcp,  # MCP-сервер роли 4
):
    history = RedisChatHistory(
        redis, settings.history_max_turns, settings.history_ttl_s, settings.history_max_tokens
    )
    assistant = Assistant(build_chat_model(settings), mcp, history, Pricing(*settings.pricing))
    # для каждого job:
    text = job.text
    transcript = None
    if job.audio_ref:
        audio, suffix = await load_audio(job.audio_ref)  # роль 2: безопасная загрузка из MAX
        try:
            transcript = await recognizer.transcribe(audio, suffix)
        except (AudioTooLongError, AudioDecodeError) as exc:
            return finish(job, answer=str(exc))  # без ретрая
        text = transcript.text
        if not text:
            return finish(job, answer="Не расслышал, повторите, пожалуйста.")
        await send_status(job, f"Распознано: {text}")  # AGENTS §11: показать расшифровку

    reply = await assistant.handle(
        job.user_id,
        text,
        reference_time=job.reference_time,  # неизменна при повторах job (AGENTS §7)
        timezone=job.timezone,  # IANA из профиля
    )
    finish(job, answer=reply.text, transcript=transcript, actions=reply.tool_calls)

await recognizer.aclose()  # при остановке worker
```

### История диалога (Redis)

| | |
|---|---|
| Ключ | `assistant:history:<user_id>`, тип list. Один элемент — один ход в JSON (LangChain `messages_to_dict`) |
| Ход | сообщение пользователя → tool calls → tool results → ответ. Хранится и обрезается целиком, поэтому пара tool call/result не разрывается |
| Хранение | последние `HISTORY_MAX_TURNS=20` ходов (`LTRIM`) |
| TTL | `HISTORY_TTL_S=604800` (7 дней). Обновляется при каждой записи хода (`EXPIRE` в той же транзакции), чтение TTL не продлевает |
| В промпт | самые новые ходы, которые помещаются в `HISTORY_MAX_TOKENS=3000`. Оценка — 3 символа на токен + 4 на сообщение, с запасом. Старые ходы отбрасываются первыми |
| Кто пишет | только `Assistant.handle()`, один раз в конце хода. Ложный ответ, пойманный guard, в историю не попадает |

Уточняющий вопрос — обычный ход в истории. Ответ пользователя («в 16:00») приходит следующим сообщением, и модель видит контекст. Отдельного состояния в Redis для этого ML-часть не держит. Состояния `processing / awaiting_clarification` и блокировки диалога ведёт роль 1.

**Согласовать с ролями 1 и 2:**
1. Ключ привязан к пользователю. Если в MAX у пользователя будет больше одного диалога с ботом (группы), нужен `assistant:history:<user_id>:<chat_id>`. Тогда `handle()` получит `chat_id`.
2. Префикс `assistant:` — роль 1 может задать общий для всех ключей проекта.
3. Сброс по команде пользователя — `assistant.reset(user_id)`.

### Проверка аргументов модели перед вызовом инструмента (для роли 1)

> **ROLE 1, проверено только локально, не в репозитории.** Логика — в нашей части,
> `app/llm/arg_checks.py`. Её нужно вызывать в агентском цикле, а это код роли 1.

**Проблема.** GigaChat-2-Max ставит напоминанию или встрече без названного времени 00:00 и создаёт запись: «Напомни завтра позвонить маме» → напоминание на полночь. Так было во всех прогонах eval, правило в промпте не помогло. MCP-сервер это не поймает: 00:00 — допустимое время.

**Что сделать в агентском цикле.** Перед отправкой `create_event` в MCP вызвать
`invented_midnight(tool_name, args, user_text)`, где `user_text` — текущая реплика пользователя (при голосе — расшифровка).
Если функция вернула текст, **не вызывать MCP**, а вернуть модели `{"error": <текст>}` как результат инструмента. Модель спросит время.
Относительное время («через 2 часа» в 22:00) и явная полночь («в полночь», «00:00», «12 ночи») пропускаются.

**Как подключено в локальном прототипе:**
- `app/agent/tools.py` — в `call()`, до `client.call_tool`;
- `app/agent/agent.py` — передача `text` в `load_user_tools`.

Оба места помечены комментарием `ROLE 1`.

Та же проверка отклоняет конец встречи (`end_time`), если пользователь не называл ни конец, ни длительность. Это `invented_end`; обе проверки вызываются через `check_args()`.

### Предложение для роли 4: конец встречи (`end_time`)

> **ROLE 4, сделано только локально в тестовой подмене (`tests/fakes`), в контракте AGENTS §8 этого нет.**

**Зачем.** Пользователи говорят «встреча с 15 до 17» или «созвон на полчаса». Сейчас конец теряется.

**Предлагаемое изменение `create_event`:**
- необязательный аргумент `end_time` (ISO 8601 со смещением);
- только для `meeting`;
- требует `datetime` и должен быть строго позже него;
- в БД — колонка `TIMESTAMPTZ NULL` с `CHECK (end IS NULL OR end > start)`.

**Если длительность не названа** — `end_time` не передаётся, запись как раньше (AGENTS §7: «не выдумывай длительность»). Промпт и `invented_end` этого придерживаются.

**`get_events_by_period`:** встреча с концом попадает в период, если пересекается с ним (`start < period_end AND end > period_start`). Встреча 23:00–01:00 видна и в «сегодня», и в «завтра». Без конца — как раньше, по моменту начала.

**Результат:** в ответе `created` появляется поле `end_time` (или `null`).

## 2. ASR: выбор между GigaAM и Yandex SpeechKit (T5)

| | GigaAM `v3_e2e_rnnt` | Yandex SpeechKit (API v1, синхронный) |
|---|---|---|
| Где | локально, CPU, torch | облако, `stt.api.cloud.yandex.net/speech/v1/stt:recognize` |
| Адаптер | `app/asr/gigaam_asr.py` | `app/asr/yandex.py` (httpx, LPCM 16 кГц, `Api-Key`) |
| Лимиты | 25 с (наш), 1 запрос одновременно | 30 с, 1 МБ, 1 канал (у нас 25 с = 800 КБ) |
| Цена | 0 ₽ за запрос, но нужен CPU/RAM сервера | 0,1626 ₽ с НДС за каждые начатые 15 с (прайс на 21.10.2026) |
| Настройка | `ASR_BACKEND=gigaam` | `ASR_BACKEND=yandex`, `YANDEX_API_KEY` (сервисный аккаунт, роль `ai.speechkit-stt.user`) |

Метрики Prometheus: `assistant_asr_audio_seconds_total{backend}`, `assistant_asr_errors_total{backend}`, `assistant_stage_latency_seconds{stage="asr"}`.

### Как сравнить

1. Записать фразы из `evals/asr_phrases.tsv`: 50 фраз с датами, временем, именами, числами, поиском и оговорками. №01–30 в тишине, №31–50 с фоновым шумом. Записи лежат в `data/asr/`, это вне git:
   `uv run python -m evals.asr_record` (Enter — старт, Enter — стоп; `--list-devices`, `--device`, `--redo 07`).
   Если фраза произнесена иначе, чем в файле, настоящий текст вносится в `data/asr/refs.tsv`.
2. Прогнать: `uv run --extra asr python -m evals.asr_run`. Yandex подключается, только если задан `YANDEX_API_KEY`. 50 фраз ≈ 51 × 0,16 ≈ 8,3 ₽ за прогон.
3. Метрики отчёта: WER и CER по корпусу; доля точных фраз; доля фраз, где все числа распознаны верно (время и даты — главное для ассистента); задержка mean/p50/p95; RTF; CPU-секунды на секунду аудио и RAM (для GigaAM).
   Перед сравнением регистр, пунктуация, «ё» и `9:30` / `9 30` / `09:30` приводятся к одному виду.

### Результаты

| Набор | ASR | N | WER | Числа | p50 / p95 | CPU-с / аудио-с | RAM |
|---|---|---|---|---|---|---|---|
| Синтез (голос Windows, без шума), только проверка раннера | GigaAM | 3 | 0% | 3/3 | 1,9 / 3,2 с | 0,95 | 1,5 ГБ после загрузки, первая фраза ~23 с |
| Живая речь, 50 фраз | GigaAM | — | — | — | — | — | — |
| Живая речь, 50 фраз | SpeechKit | — | — | — | — | — | — |

**Выбор пока не сделан:** нет живых записей и ключа SpeechKit. Предварительно: GigaAM бесплатен и точен на чистой речи, но занимает ~1,5 ГБ RAM и почти 1 ядро на секунду аудио (CPU 6 ядер). На маленьком VPS это нужно проверить отдельно (AGENTS §12).

## 3. Запуск, проверки и eval

Раздел перенесён из README ветки `feature/ml-llm-asr`: README `main` описывает весь проект.

### Схема

```
голос ─▶ ASR (GigaAM | SpeechKit) ─▶ текст ─▶ Agent (LangChain + GigaChat) ─▶ текстовый ответ
                                                │   ▲
                                  история в Redis   │ MCP tools
                                                ▼   │
              create_event / get_events_by_period / get_event_by_query ─▶ MCP-сервер роли 4
```

Пока сервера роли 4 нет, тесты, eval и dev-демо работают с заглушкой `tests/fakes/mcp_server.py` (SQLite)
с тем же контрактом AGENTS §8: `datetime` либо `date`, периоды `[start_time, end_time)`,
статусы задач `todo / in_progress / done`. `delete_event` — дополнительный инструмент, выключен
флагом `ENABLE_DELETE_EVENT=false`, пока роль 4 не согласует контракт.

### Запуск

Нужны Python 3.12 и [uv](https://docs.astral.sh/uv/). Для голоса — ffmpeg в PATH
(`winget install Gyan.FFmpeg`), для истории — Docker.

```bash
uv sync --extra asr              # без --extra asr ставится только текстовая часть, без torch
cp .env.example .env             # вписать GIGACHAT_CREDENTIALS
docker compose up -d redis postgres   # история диалога — в Redis, записи — в PostgreSQL
```

Dev-демо (не продукт; пользователь определяется cookie, `user_id` в продукте берётся из MAX):

```bash
uv run python -m tools.demo.cli --debug                       # текстовый чат в терминале
uv run --extra asr uvicorn tools.demo.web:app --port 8000     # микрофон в браузере, /metrics
```

### Проверки

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

Тесты работают без LLM и без сети: скриптованная модель и MCP-заглушка в памяти.
mypy (strict) проверяет `app`, `evals`, `tools`, `tests/fakes`.

### Eval

```bash
uv run python -m evals.run --repeat 3                  # GigaChat из .env, все кейсы
uv run python -m evals.run --split holdout             # только отложенная выборка
uv run python -m evals.run -k today_ search_           # кейсы с такими префиксами id
uv run python -m evals.run --enable-delete             # с delete_event и кейсами удаления
uv run python -m evals.run --provider cloudru --model ai-sage/GigaChat3.5-432B-A28B-Reasoning  # платно
```

У каждого кейса своя временная опора и часовой пояс, поэтому проверки относительных дат детерминированы.
Кейс проверяет инструмент и аргументы: тип, точное время или дату задачи, полуинтервал периода, ключевые
слова поиска, отсутствие лишних вызовов и пригодность ответа для озвучки. JSON-отчёт — в `evals/reports/`
(вне git), сводная таблица моделей — `python -m evals.summary --write docs/ML_EVAL.md`.

Cloud.ru (`evals/cloudru.py`) — только способ сравнить модели. Продуктовый стек — GigaChat API (AGENTS §3).

### Решения, принятые самостоятельно

- **`user_id` не передаётся через LLM.** Он убирается из схемы инструментов и подставляется кодом,
  поэтому промпт-инъекцией нельзя прочитать чужие записи.
- **Календарь в промпте.** Помимо времени и дня недели в промпт передаётся таблица дат на две недели
  с пометками «эта / следующая неделя». Модели надёжнее находят «в пятницу» по таблице, чем считают сами.
  «Через N минут/часов» посчитано заранее, с переходом через полночь.
- **Недостающее время проверяется дважды.** Промпт велит спросить время для встречи или напоминания.
  Если модель всё же вызовет инструмент без времени, сервер вернёт ошибку, и модель задаст вопрос.
- **Тип по умолчанию.** «Напомни» — reminder, встреча или созвон — meeting, остальное — task.
  Задача с датой без времени передаётся как `date`; время не придумывается.
- **Защита от ложного «записал»** (`app/agent/guard.py`). Если ответ говорит «записал», а успешного
  `create_event` не было, агент один раз повторяет запрос с указанием на ошибку, затем отдаёт честный
  ответ. Так же для «удалил» без `delete_event`. Ложный ответ в историю не попадает.
- **Кэш промпта GigaChat.** Неизменные правила стоят в начале, время запроса — в конце,
  и для каждого пользователя передаётся `X-Session-ID` (`app/llm/model.py:llm_session`).
- **Стоимость** считается из `usage_metadata` по цене за 1000 токенов из настроек и пишется в Prometheus
  гистограммой.
- **Поиск в заглушке** — совпадение по основам слов. В продукте это PostgreSQL FTS роли 4; не семантический поиск.

### История сравнения моделей (до T2, прототипный контракт)

Cloud.ru, temperature 0.1, 22 кейса × 3, 19.10.2026, прайс v260915 с НДС:

| Модель | Кейсы | p50 / p95 | ₽ за запрос p50 / p95 |
|---|---|---|---|
| `ai-sage/GigaChat3.5-432B-A28B-Reasoning` + guard | 66/66 | 4,7 / 9,2 с | 0,41 / 0,56 |
| `GigaChat/GigaChat-3-Pro` | 46/66 | 1,6 / 3,0 с | 0,46 / 0,49 |
| `ai-sage/GigaChat3-10B-A1.8B` | 48/66 | 0,7 / 1,7 с | 0,06 / 0,10 |

Прямой GigaChat API (пакет B2B), 3 прогона:

| Модель | Кейсы | p50 / p95 | Типичная ошибка |
|---|---|---|---|
| `GigaChat-2-Max` + разметка недель, таймаут 12 с | 66/66 | 2,7 / 3,6 с | — |
| `GigaChat-2-Pro` | 58/66 | 2,7 / 4,2 с | встреча без времени создаётся на 00:00 |
| `GigaChat-3-Pro` | 55/66 | 2,1 / 3,4 с | придумывает время встречи, путает дни |
| `GigaChat-3-Lightning` | 51/66 | 1,2 / 3,0 с | не спрашивает время, ошибается с датами |

Текущие результаты на 125 кейсах — в [ML_EVAL.md](ML_EVAL.md).
