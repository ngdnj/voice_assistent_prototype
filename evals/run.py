"""Run eval cases against the real LLM and report accuracy, latency, tokens and cost.

    uv run python -m evals.run                       # all cases
    uv run python -m evals.run --split holdout       # final measurement only
    uv run python -m evals.run --category search     # one request class
    uv run python -m evals.run -k today search_      # cases whose id starts with a prefix
    uv run python -m evals.run --repeat 3 --temperature 0.3

Each case gets a fresh in-memory storage and history, so cases are independent.
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
from zoneinfo import ZoneInfo

from app.agent.agent import AgentReply, Assistant, Pricing
from app.agent.history import InMemoryChatHistory
from fastmcp import Client
from langchain_core.language_models import BaseChatModel
from tests.fakes.events import EventType
from tests.fakes.mcp_server import build_server
from tests.fakes.storage import EventStorage

from app.config import Settings, get_settings
from app.llm.model import build_chat_model
from evals.cases import CASES, Case, Category, Split
from evals.checks import Check, speech_issues
from evals.cloudru import CloudRuSettings, build_cloudru_model
from evals.stats import summarize

REPORTS_DIR = Path(__file__).parent / "reports"
INFRA_ATTEMPTS = 3
# Without an explicit expectation nothing may change the user's records.
MUTATING_TOOLS = ("create_event", "delete_event")


@dataclass
class CaseResult:
    id: str
    category: str
    split: str
    passed: bool
    errors: list[str]
    answer: str
    tool_calls: list[dict[str, Any]]
    latency_s: float
    cost_rub: float
    input_tokens: int
    output_tokens: int
    retried: bool = False
    # API/network failure, not a model mistake; excluded from accuracy.
    infra_error: bool = False
    timezone: str = ""
    turns: list[str] = field(default_factory=list)


def _first_error(checks: list[Check], args: dict[str, Any]) -> str | None:
    return next((e for check in checks if (e := check(args))), None)


def _evaluate(case: Case, reply: AgentReply) -> list[str]:
    expect = case.expect
    errors: list[str] = []
    forbidden = set(expect.forbid) or (set(MUTATING_TOOLS) if expect.tool is None else set())
    for call in reply.tool_calls:
        if call.name in forbidden:
            errors.append(f"не должно быть {call.name}, вызвано: {call.args}")
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
    errors += speech_issues(reply.text)
    return errors


def _seed_time(value: str | None) -> tuple[datetime | None, date | None]:
    """A seed time is an ISO moment or, for a task without time, a plain YYYY-MM-DD date."""
    if not value:
        return None, None
    if len(value) == len("YYYY-MM-DD"):
        return None, date.fromisoformat(value)
    return datetime.fromisoformat(value), None


async def _run_case(case: Case, settings: Settings, model: BaseChatModel) -> CaseResult:
    tz = ZoneInfo(case.timezone)
    storage = EventStorage(":memory:", tz)
    for seed in case.seed:
        when, due_date = _seed_time(seed.when)
        storage.create("eval", EventType(seed.event_type), seed.name, seed.description, when, due_date)

    enable_delete = settings.enable_delete_event
    async with Client(build_server(storage, tz, enable_delete=enable_delete)) as client:
        assistant = Assistant(
            model,
            client,
            InMemoryChatHistory(10),
            Pricing(*settings.pricing),
            enable_delete=enable_delete,
        )
        # Every turn keeps the case's reference time, like a retried job keeps the original one.
        replies = [
            await assistant.handle("eval", turn, reference_time=case.now, timezone=case.timezone)
            for turn in case.turns
        ]
    storage.close()

    last = replies[-1]
    errors = _evaluate(case, last)
    return CaseResult(
        id=case.id,
        category=case.category,
        split=case.split,
        passed=not errors,
        errors=errors,
        answer=last.text,
        tool_calls=[{"name": c.name, "args": c.args} for c in last.tool_calls],
        latency_s=sum(r.latency_s for r in replies),
        cost_rub=sum(r.cost_rub for r in replies),
        input_tokens=sum(r.input_tokens for r in replies),
        output_tokens=sum(r.output_tokens for r in replies),
        retried=any(r.retried for r in replies),
        timezone=case.timezone,
        turns=case.turns,
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
    print(f"\n== {title}: {summary['passed']}/{summary['n']} ({summary['accuracy']:.0%})")
    for category, counts in summary["by_category"].items():
        print(f"   {category:<10} {counts['passed']}/{counts['n']}")
    if summary["infra_errors"]:
        print(f"   сбои API/сети, не учтены в точности: {summary['infra_errors']}")
    print(f"   латентность, с:    {_fmt(summary['latency_s'], 2)}")
    print(f"   стоимость, ₽:      {_fmt(summary['cost_rub'], 3)}")
    print(f"   токены вход:       {_fmt(summary['input_tokens'], 0)}")
    print(f"   токены выход:      {_fmt(summary['output_tokens'], 0)}")


def _print_report(results: list[CaseResult], summaries: dict[str, dict[str, Any]]) -> None:
    for r in results:
        mark = ("ERR " if r.infra_error else "PASS" if r.passed else "FAIL") + ("↻" if r.retried else " ")
        print(f"[{mark}]{r.id:<34} {r.latency_s:5.1f}s {r.cost_rub:6.3f}₽  {r.answer[:70]!r}")
        for error in r.errors:
            print(f"        - {error}")
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
        help="give the agent delete_event and run the delete cases (not an MVP tool, off by default)",
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
        and (args.split == "all" or c.split == args.split)
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
    summaries = {k: v for k, v in summaries.items() if v["n"] or v["infra_errors"]}
    summaries["all"] = summarize(rows)
    _print_report(results, summaries)

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = {
        "provider": args.provider,
        "model": model_name,
        "temperature": settings.llm_temperature,
        "repeat": args.repeat,
        "enable_delete": settings.enable_delete_event,
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
