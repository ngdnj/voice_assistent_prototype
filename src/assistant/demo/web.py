"""Minimal web demo: record voice or type text, see the answer, tool calls and cost.

    uv run --extra asr uvicorn assistant.demo.web:app --port 8000

The demo identifies a browser by a random cookie. In the real product user_id
comes from MAX authentication, never from the client.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

from fastapi import Cookie, FastAPI, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from prometheus_client import make_asgi_app
from pydantic import BaseModel, Field

from assistant import metrics
from assistant.asr import AudioTooLongError, SpeechRecognizer, build_recognizer
from assistant.config import get_settings
from assistant.llm.agent import AgentReply, Assistant
from assistant.runtime import open_assistant

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
USER_COOKIE = "assistant_demo_user"
MAX_AUDIO_BYTES = 5 * 1024 * 1024
AUDIO_SUFFIXES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/mp4": ".m4a"}


class State:
    assistant: Assistant
    recognizer: SpeechRecognizer


state = State()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    state.recognizer = build_recognizer(settings)
    async with open_assistant(settings) as assistant:
        state.assistant = assistant
        yield


app = FastAPI(title="Assistant ML demo", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def _user_id(response: Response, cookie: str | None) -> str:
    if cookie:
        return cookie
    user_id = uuid.uuid4().hex
    response.set_cookie(USER_COOKIE, user_id, httponly=True, samesite="strict")
    return user_id


async def _answer(user_id: str, text: str) -> dict[str, Any]:
    try:
        reply: AgentReply = await state.assistant.handle(user_id, text)
    except Exception as exc:
        metrics.AGENT_ERRORS.labels(type(exc).__name__).inc()
        logger.exception("agent failed")
        raise HTTPException(502, "Ассистент сейчас недоступен, попробуйте ещё раз") from exc
    return asdict(reply)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/chat")
async def chat(
    body: ChatRequest, response: Response, user: Annotated[str | None, Cookie(alias=USER_COOKIE)] = None
) -> dict[str, Any]:
    return await _answer(_user_id(response, user), body.text)


@app.post("/api/voice")
async def voice(
    audio: UploadFile, response: Response, user: Annotated[str | None, Cookie(alias=USER_COOKIE)] = None
) -> dict[str, Any]:
    content_type = (audio.content_type or "").split(";")[0]
    suffix = AUDIO_SUFFIXES.get(content_type)
    if suffix is None:
        raise HTTPException(415, f"Неподдерживаемый формат {content_type!r}")
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Слишком большой файл")
    try:
        transcript = await state.recognizer.transcribe(data, suffix)
    except AudioTooLongError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not transcript.text:
        return {"transcript": "", "asr_latency_s": transcript.latency_s, "text": "Не расслышал, повторите."}
    reply = await _answer(_user_id(response, user), transcript.text)
    return {"transcript": transcript.text, "asr_latency_s": transcript.latency_s, **reply}


@app.post("/api/reset")
async def reset(response: Response, user: Annotated[str | None, Cookie(alias=USER_COOKIE)] = None) -> None:
    await state.assistant.reset(_user_id(response, user))
