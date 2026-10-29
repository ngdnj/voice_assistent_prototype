"""Run eval cases against the real LLM and report accuracy, latency, tokens and cost.

    uv run python -m evals.run                       # all cases
    uv run python -m evals.run --split holdout       # final measurement only
    uv run python -m evals.run --category search     # one request class
    uv run python -m evals.run -k today search_      # cases whose id starts with a prefix
    uv run python -m evals.run --repeat 3 --temperature 0.3

Each case gets an isolated owner in a dedicated test PostgreSQL and in-memory history.
Records are removed after the case; MCP schemas and business rules are the real implementation.
A JSON report is written to evals/reports/; `python -m evals.summary` builds the model table.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, get_args

from langchain_core.language_models import BaseChatModel

from app.agent.agent import AgentReply, Assistant, Pricing, ToolCall
from app.agent.history import InMemoryChatHistory
from app.config import Settings, get_settings
from app.llm.model import build_chat_model
from evals.backend import evaluation_backend
from evals.cases import CASES, Case, Category, Split
from evals.checks import Check, speech_issues
from evals.cloudru import CloudRuSettings, build_cloudru_model
from evals.stats import summarize

REPORTS_DIR = Path(__file__).parent / "reports"
INFRA_ATTEMPTS = 3
# Without an explicit expectation nothing may change the user's records.
MUTATING_TOOLS = ("create_event", "reschedule_meeting", "update_event", "delete_event")


@dataclass
class CaseResult:
    id: str
    category: str
    split: str
    # Strict: every expectation of the case holds.
    passed: bool
    errors: list[str]
    answer: str
    tool_calls: list[dict[str, Any]]
    latency_s: float
    cost_rub: float
    input_tokens: int
    output_tokens: int
    # By behaviour: a forbidden create_event that was rejected (by arg checks or the server) is not a
    # failure, since the user's records did not change and the final answer is still checked.
    passed_behavior: bool = False
    behavior_errors: list[str] = field(default_factory=list)
    # Prompt prefix served from the provider's cache; not in input_tokens and not billed by GigaChat.
    cached_input_tokens: int = 0
    retried: bool = False
    # API/network failure, not a model mistake; excluded from accuracy.
    infra_error: bool = False
    timezone: str = ""
    turns: list[str] = field(default_factory=list)
    # Per turn: what the user said, every tool call with the server's answer, the final reply.
    trace: list[dict[str, Any]] = field(default_factory=list)


TRACE_RESULT_CHARS = 300


def _call_trace(call: ToolCall) -> dict[str, Any]:
    result = json.dumps(call.result, ensure_ascii=False) if call.result is not None else "нет ответа"
    return {"name": call.name, "args": call.args, "ok": call.ok, "result": result[:TRACE_RESULT_CHARS]}


def build_trace(turns: list[str], replies: list[AgentReply]) -> list[dict[str, Any]]:
    """The whole dialog of a case, for finding out *why* a check failed."""
    return [
        {
            "user": text,
            "calls": [_call_trace(c) for c in reply.tool_calls],
            "answer": reply.text,
            "retried": reply.retried,
        }
        for text, reply in zip(turns, replies, strict=True)
    ]


def format_trace(trace: list[dict[str, Any]]) -> list[str]:
    lines = []
    for i, step in enumerate(trace, 1):
        lines.append(f"        {i}. пользователь: {step['user']}")
        for call in step["calls"]:
            mark = "ok" if call["ok"] else "ОШИБКА"
            args = json.dumps(call["args"], ensure_ascii=False)
            lines.append(f"           ↳ {call['name']}({args}) → {mark}: {call['result']}")
        retried = " (после повтора guard)" if step["retried"] else ""
        lines.append(f"           ответ{retried}: {step['answer']}")
    return lines


def _first_error(checks: list[Check], args: dict[str, Any]) -> str | None:
    return next((e for check in checks if (e := check(args))), None)


def _evaluate(case: Case, reply: AgentReply, *, by_behavior: bool = False) -> list[str]:
    """Errors of the last turn; `by_behavior` excuses forbidden changes that did not happen."""
    expect = case.expect
    errors: list[str] = []
    forbidden = set(expect.forbid)
    if expect.tool in {None, "get_events_by_period", "get_event_by_query"}:
        forbidden.update(MUTATING_TOOLS)
    for call in reply.tool_calls:
        if by_behavior and call.name in MUTATING_TOOLS and not call.ok:
            continue
        if call.name in forbidden:
            errors.append(f"не должно быть {call.name}, вызвано: {call.args}")
        if call.result is not None and "error" in call.result:
            errors.append(f"инструмент {call.name} вернул ошибку")
        if call.name == "delete_event" and call.result is not None and "deleted" in call.result:
            deleted_name = str(call.result["deleted"].get("name", "")).lower()
            for words in expect.answer_mentions:
                if not any(word.lower() in deleted_name for word in words.split("|")):
                    errors.append("удалена запись, не соответствующая ожидаемой")
    if expect.tool is not None:
        calls = [c for c in reply.tool_calls if c.name == expect.tool]
        if not calls:
            names = [c.name for c in reply.tool_calls] or "нет вызовов"
            errors.append(f"ожидался {expect.tool}, вызвано: {names}")
        else:
            errors += [e for check in expect.checks if (e := check(calls[-1].args))]
            for group in expect.also:
                misses = [_first_error(group, c.args) for c in calls]
                if all(misses):
                    errors.append(f"нет подходящего {expect.tool}: {misses[0]}")
    answer = reply.text.lower()
    for words in expect.answer_mentions:  # "a|b" means any of the alternatives
        if not any(w.lower() in answer for w in words.split("|")):
            errors.append(f"в ответе нет «{words}»")
    errors += [f"в ответе есть «{word}»" for word in expect.answer_absent if word.lower() in answer]
    # AgentReply is displayed in chat. TTS still has its separate to_speech_text boundary.
    errors += speech_issues(reply.text, allow_lists=True)
    return errors


def _seed_time(value: str | None) -> tuple[datetime | None, date | None]:
    """A seed time is an ISO moment or, for a task without time, a plain YYYY-MM-DD date."""
    if not value:
        return None, None
    if len(value) == len("YYYY-MM-DD"):
        return None, date.fromisoformat(value)
    return datetime.fromisoformat(value), None


async def _run_case(case: Case, settings: Settings, model: BaseChatModel) -> CaseResult:
    async with evaluation_backend(case.now, case.timezone) as client:
        for number, seed in enumerate(case.seed):
            when, due_date = _seed_time(seed.when)
            arguments: dict[str, Any] = {
                "event_type": seed.event_type,
                "name": seed.name,
                "description": seed.description,
            }
            if when is not None:
                arguments["datetime"] = when.isoformat()
            if due_date is not None:
                arguments["date"] = due_date.isoformat()
            await client.seed(arguments, action_id=f"seed-{number}")
        assistant = Assistant(
            model,
            client,
            InMemoryChatHistory(10),
            Pricing(*settings.pricing),
        )
        # Every turn keeps the case's reference time, like a retried job keeps the original one.
        replies = [
            await assistant.handle(client.owner, turn, reference_time=case.now, timezone=case.timezone)
            for turn in case.turns
        ]

    last = replies[-1]
    errors = _evaluate(case, last)
    behavior_errors = _evaluate(case, last, by_behavior=True)
    return CaseResult(
        id=case.id,
        category=case.category,
        split=case.split,
        passed=not errors,
        errors=errors,
        answer=last.text,
        tool_calls=[{"name": c.name, "args": c.args, "ok": c.ok} for c in last.tool_calls],
        latency_s=sum(r.latency_s for r in replies),
        cost_rub=sum(r.cost_rub for r in replies),
        input_tokens=sum(r.input_tokens for r in replies),
        output_tokens=sum(r.output_tokens for r in replies),
        passed_behavior=not behavior_errors,
        behavior_errors=behavior_errors,
        # The field comes with role 1's agent patch (docs/ML_PROPOSALS.md); without it the cache is 0.
        cached_input_tokens=sum(getattr(r, "cached_input_tokens", 0) for r in replies),
        retried=any(r.retried for r in replies),
        timezone=case.timezone,
        turns=case.turns,
        trace=build_trace(case.turns, replies),
    )


async def _run_safe(case: Case, settings: Settings, model: BaseChatModel) -> CaseResult:
    # Model mistakes never raise: an exception here is an API or network failure.
    error = ""
    for attempt in range(INFRA_ATTEMPTS):
        try:
            return await _run_case(case, settings, model)
        except Exception as exc:  # one broken case must not stop the whole run
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            await asyncio.sleep(2 * (attempt + 1))
    return CaseResult(
        case.id, case.category, case.split, False, [error], "", [], 0, 0, 0, 0, infra_error=True
    )


def _fmt(stats: dict[str, float], digits: int) -> str:
    return (
        f"mean={stats['mean']:.{digits}f} median={stats['median']:.{digits}f} p95={stats['p95']:.{digits}f}"
    )


def _print_summary(title: str, summary: dict[str, Any]) -> None:
    print(
        f"\n== {title}: {summary['passed']}/{summary['n']} ({summary['accuracy']:.0%}), "
        f"по поведению {summary['passed_behavior']}/{summary['n']} ({summary['accuracy_behavior']:.0%})"
    )
    for category, counts in summary["by_category"].items():
        print(f"   {category:<10} {counts['passed']}/{counts['n']}")
    if summary["infra_errors"]:
        print(f"   сбои API/сети, не учтены в точности: {summary['infra_errors']}")
    print(f"   латентность, с:    {_fmt(summary['latency_s'], 2)}")
    print(f"   стоимость, ₽:      {_fmt(summary['cost_rub'], 3)}")
    print(f"   токены вход:       {_fmt(summary['input_tokens'], 0)}  (без кэша, тарифицируются)")
    print(f"   токены кэш:        {_fmt(summary['cached_input_tokens'], 0)}")
    print(f"   токены вход всего: {_fmt(summary['total_input_tokens'], 0)}")
    print(f"   токены выход:      {_fmt(summary['output_tokens'], 0)}")


def _print_report(
    results: list[CaseResult], summaries: dict[str, dict[str, Any]], trace: str = "off"
) -> None:
    for r in results:
        # BHV: strict failure, but no record changed and the answer is right (CaseResult.passed_behavior).
        verdict = "PASS" if r.passed else "BHV " if r.passed_behavior else "FAIL"
        mark = ("ERR " if r.infra_error else verdict) + ("↻" if r.retried else " ")
        print(f"[{mark}]{r.id:<34} {r.latency_s:5.1f}s {r.cost_rub:6.3f}₽  {r.answer[:70]!r}")
        for error in r.errors:
            print(f"        - {error}")
        if trace == "all" or (trace == "failed" and not r.passed and not r.infra_error):
            print("\n".join(format_trace(r.trace)))
    by_case: dict[str, list[CaseResult]] = {}
    for r in results:
        if not r.infra_error:
            by_case.setdefault(r.id, []).append(r)
    flaky = {cid: items for cid, items in by_case.items() if 0 < sum(i.passed for i in items) < len(items)}
    if flaky:
        print("\nНестабильные кейсы:")
        for cid, items in flaky.items():
            print(f"  {cid}: {sum(i.passed for i in items)}/{len(items)}")
    for title, summary in summaries.items():
        _print_summary(title, summary)
    print(f"\nСтоимость прогона, ₽: {sum(r.cost_rub for r in results):.2f}")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-k", nargs="*", default=[], help="run only cases whose id starts with these prefixes"
    )
    parser.add_argument("--split", choices=[*get_args(Split), "all"], default="all")
    parser.add_argument("--category", nargs="*", choices=get_args(Category), default=[])
    parser.add_argument("--repeat", type=int, default=1, help="run each case N times to measure stability")
    parser.add_argument("--temperature", type=float, help="override LLM_TEMPERATURE")
    parser.add_argument(
        "--provider",
        choices=["gigachat", "cloudru"],
        default="gigachat",
        help="gigachat — product stack; cloudru — only for comparison, costs money",
    )
    parser.add_argument("--model", help="override the model of the provider")
    parser.add_argument("--concurrency", type=int, default=2, help="parallel cases (mind API rate limits)")
    parser.add_argument("--price-in", type=float, help="RUB per 1000 input tokens for this model")
    parser.add_argument("--price-out", type=float, help="RUB per 1000 output tokens for this model")
    parser.add_argument(
        "--enable-delete",
        action="store_true",
        help="include deletion eval cases; the product agent already supports deletion",
    )
    parser.add_argument(
        "--trace",
        nargs="?",
        const="failed",
        default="off",
        choices=["off", "failed", "all"],
        help="print each turn: tool calls with the server's answer and the reply (default: failed cases)",
    )
    args = parser.parse_args()

    overrides: dict[str, Any] = {}
    if args.temperature is not None:
        overrides["llm_temperature"] = args.temperature
    if args.price_in is not None:
        overrides["llm_price_input_rub_per_1k"] = args.price_in
    if args.price_out is not None:
        overrides["llm_price_output_rub_per_1k"] = args.price_out
    if args.enable_delete:
        overrides["enable_delete_event"] = True
    settings = get_settings().model_copy(update=overrides)
    if args.model:
        settings = settings.with_llm_model(args.model)
    cloudru = CloudRuSettings()
    if args.provider == "cloudru" and args.model:
        cloudru = cloudru.model_copy(update={"cloudru_model": args.model})
    model_name = cloudru.cloudru_model if args.provider == "cloudru" else settings.llm_model

    cases = [
        c
        for c in CASES
        if (not args.k or any(c.id.startswith(p) for p in args.k))
        # "all" is the measured §15 dataset; debug cases run only when asked for.
        and (c.split == args.split or (args.split == "all" and (c.split != "debug" or args.k)))
        and (not args.category or c.category in args.category)
        and c.runs_with(settings.enable_delete_event)
    ]
    if not cases:
        print("Нет кейсов под фильтр", file=sys.stderr)
        return 2

    # One model (and one OAuth token) for the whole run: per-case clients hit the auth rate limit.
    model = (
        build_cloudru_model(settings, cloudru) if args.provider == "cloudru" else build_chat_model(settings)
    )
    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded(case: Case) -> CaseResult:
        async with semaphore:
            return await _run_safe(case, settings, model)

    results = await asyncio.gather(*(bounded(c) for c in cases for _ in range(args.repeat)))
    rows = [asdict(r) for r in results]
    summaries = {split: summarize(r for r in rows if r["split"] == split) for split in get_args(Split)}
    # Debug cases are hard on purpose: keep them out of the overall accuracy.
    measured = [r for r in rows if r["split"] != "debug"]
    summaries = {k: v for k, v in summaries.items() if v["n"] or v["infra_errors"]}
    if measured:
        summaries["all"] = summarize(measured)
    _print_report(results, summaries, args.trace)

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = {
        "provider": args.provider,
        "model": model_name,
        "temperature": settings.llm_temperature,
        "repeat": args.repeat,
        "enable_delete": settings.enable_delete_event,
        # Older reports may say "stub" (the SQLite test double); evals.summary keeps them apart.
        "mcp": "postgres",
        "filter": {"split": args.split, "category": args.category, "k": args.k},
        "pricing_rub_per_1k": {"input": settings.pricing[0], "output": settings.pricing[1]},
        "summary": summaries,
        "results": rows,
    }
    model_slug = model_name.replace("/", "_")
    path = REPORTS_DIR / f"{stamp}-{model_slug}-t{settings.llm_temperature}-{args.split}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nОтчёт: {path}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
