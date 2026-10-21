# ML-часть: ASR + LLM-агент

Владелец — роль 3 (AGENTS §5). Здесь интерфейсы, которые роль 1 подключает к очереди и боту,
и порядок выбора ASR. Пути указаны до переноса в `app/` (T1): `assistant.asr` → `app.asr`,
`assistant.llm.{agent,history,guard,tools}` → `app.agent`.

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
class Transcript:  # assistant.asr
    text: str  # "" — речь не распознана
    latency_s: float  # декодирование + распознавание
    audio_s: float  # длительность аудио, единица стоимости ASR


@dataclass
class AgentReply:  # assistant.llm.agent
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
from assistant.asr import AsrError, AudioDecodeError, AudioTooLongError, build_recognizer
from assistant.config import get_settings
from assistant.runtime import open_assistant

settings = get_settings()
recognizer = build_recognizer(settings)  # один на процесс worker

async with open_assistant(settings) as assistant:  # модель, MCP client, Redis-история
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

## 2. ASR: выбор между GigaAM и Yandex SpeechKit (T5)

| | GigaAM `v3_e2e_rnnt` | Yandex SpeechKit (API v1, синхронный) |
|---|---|---|
| Где | локально, CPU, torch | облако, `stt.api.cloud.yandex.net/speech/v1/stt:recognize` |
| Адаптер | `asr/gigaam_asr.py` | `asr/yandex.py` (httpx, LPCM 16 кГц, `Api-Key`) |
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
