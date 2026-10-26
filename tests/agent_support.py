"""Scripted model only: no external LLM calls in automated tests."""

from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import Field


class ScriptedModel(FakeMessagesListChatModel):
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    schemas: list[dict[str, Any]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.schemas = list(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def tool(name: str = "create_event", arguments: dict | None = None, *, call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        "",
        tool_calls=[
            {
                "name": name,
                "args": arguments if arguments is not None else {"event_type": "task", "name": "Отчёт"},
                "id": call_id,
            }
        ],
    )
