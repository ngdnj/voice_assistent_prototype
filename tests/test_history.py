"""Dialog history limits for the agent runner (AGENTS §10): whole turns within a token budget."""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from assistant.llm.history import InMemoryChatHistory, estimate_tokens, fit_to_budget


def tool_turn(i: int, answer_chars: int = 30) -> list[BaseMessage]:
    call = {"id": f"c{i}", "name": "create_event", "args": {"event_type": "task", "name": f"дело {i}"}}
    return [
        HumanMessage(f"запиши дело {i}"),
        AIMessage("", tool_calls=[call]),
        ToolMessage('{"created": {"id": 1}}', tool_call_id=f"c{i}", name="create_event"),
        AIMessage("г" * answer_chars),
    ]


def asked(messages: list[BaseMessage]) -> list[str]:
    return [m.text for m in messages if isinstance(m, HumanMessage)]


def test_budget_keeps_newest_whole_turns() -> None:
    turns = [tool_turn(i) for i in range(5)]
    per_turn = estimate_tokens(turns[0])
    kept = fit_to_budget(turns, max_tokens=per_turn * 2 + per_turn // 2)
    assert asked(kept) == ["запиши дело 3", "запиши дело 4"]


def test_tool_call_is_never_separated_from_its_result() -> None:
    turns = [tool_turn(i) for i in range(5)]
    for budget in range(0, estimate_tokens([m for t in turns for m in t]) + 1, 7):
        kept = fit_to_budget(turns, budget)
        assert not kept or isinstance(kept[0], HumanMessage)
        call_ids = {c["id"] for m in kept if isinstance(m, AIMessage) for c in m.tool_calls}
        result_ids = {m.tool_call_id for m in kept if isinstance(m, ToolMessage)}
        assert call_ids == result_ids


def test_turn_larger_than_budget_is_dropped_with_everything_older() -> None:
    turns = [tool_turn(0), tool_turn(1, answer_chars=30_000), tool_turn(2)]
    kept = fit_to_budget(turns, max_tokens=estimate_tokens(turns[2]) + 100)
    assert asked(kept) == ["запиши дело 2"]


def test_estimate_counts_tool_call_arguments() -> None:
    plain = AIMessage("")
    with_call = AIMessage("", tool_calls=[{"id": "c", "name": "create_event", "args": {"name": "x" * 300}}])
    assert estimate_tokens([with_call]) >= estimate_tokens([plain]) + 100


async def test_in_memory_history_applies_budget_on_load() -> None:
    history = InMemoryChatHistory(max_turns=20, max_tokens=estimate_tokens(tool_turn(0)))
    for i in range(3):
        await history.append_turn("u1", tool_turn(i))
    assert asked(await history.load("u1")) == ["запиши дело 2"]
    assert await history.load("u2") == []
