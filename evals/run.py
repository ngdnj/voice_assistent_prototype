"""Run eval cases against the real LLM and report accuracy, latency and cost.

    uv run python -m evals.run                       # all cases
    uv run python -m evals.run -k today search_      # cases whose id starts with a prefix
    uv run python -m evals.run --repeat 3 --temperature 0.3

Each case gets a fresh in-memory storage and history, so cases are independent.
A JSON report is written to evals/reports/ for comparing prompt/model versions.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp import Client

from assistant.config import Settings, get_settings
from assistant.events.models import EventType
from assistant.events.storage import EventStorage
from assistant.llm.agent import AgentReply, Assistant, Pricing
from assistant.llm.history import InMemoryChatHistory
from assistant.llm.model import build_chat_model
from assistant.mcp_server.server import build_server
from assistant.metrics import quantiles
from evals.cases import CASES, NOW, TZ, Case
from evals.checks import speech_issues

REPORTS_DIR = Path(__file__).parent / "reports"


@dataclass
class CaseResult:
    id: str
    category: str
    passed: bool
    errors: list[str]
    answer: str
    tool_calls: list[dict[str, Any]]
    latency_s: float
    cost_rub: float
    tokens: int
    turns: list[str] = field(default_factory=list)


def _evaluate(case: Case, reply: AgentReply) -> list[str]:
    expect = case.expect
    errors: list[str] = []
    if expect.tool is None:
        created = [c for c in reply.tool_calls if c.name == "create_event"]
        if created:
            errors.append(f"не должно быть create_event, вызвано: {created[-1].args}")
    else:
        calls = [c for c in reply.tool_calls if c.name == expect.tool]
        if not calls:
            names = [c.name for c in reply.tool_calls] or "нет вызовов"
            errors.append(f"ожидался {expect.tool}, вызвано: {names}")
        else:
            errors += [e for check in expect.checks if (e := check(calls[-1].args))]
    answer = reply.text.lower()
    errors += [f"в ответе нет «{w}»" for w in expect.answer_mentions if w.lower() not in answer]
    errors += speech_issues(reply.text)
    return errors


async def _run_case(case: Case, settings: Settings) -> CaseResult:
    storage = EventStorage(":memory:", TZ)
    for seed in case.seed:
        when = datetime.fromisoformat(seed.when) if seed.when else None
        storage.create("eval", EventType(seed.event_type), seed.name, seed.description, when)

    model = build_chat_model(settings)
    async with Client(build_server(storage, TZ)) as client:
        assistant = Assistant(model, client, InMemoryChatHistory(10), TZ, Pricing(*settings.pricing))
        replies = [await assistant.handle("eval", turn, now=NOW) for turn in case.turns]
    storage.close()

    last = replies[-1]
    errors = _evaluate(case, last)
    return CaseResult(
        id=case.id,
        category=case.category,
        passed=not errors,
        errors=errors,
        answer=last.text,
        tool_calls=[{"name": c.name, "args": c.args} for c in last.tool_calls],
        latency_s=sum(r.latency_s for r in replies),
        cost_rub=sum(r.cost_rub for r in replies),
        tokens=sum(r.input_tokens + r.output_tokens for r in replies),
        turns=case.turns,
    )


async def _run_safe(case: Case, settings: Settings) -> CaseResult:
    try:
        return await _run_case(case, settings)
    except Exception as exc:  # one broken case must not stop the whole run
        return CaseResult(case.id, case.category, False, [f"{type(exc).__name__}: {exc}"], "", [], 0, 0, 0)


def _print_report(results: list[CaseResult]) -> None:
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"[{mark}] {r.id:<32} {r.latency_s:5.1f}s {r.cost_rub:6.3f}₽  {r.answer[:70]!r}")
        for error in r.errors:
            print(f"        - {error}")
    print()
    by_category: dict[str, list[CaseResult]] = {}
    for r in results:
        by_category.setdefault(r.category, []).append(r)
    for category, items in sorted(by_category.items()):
        print(f"{category:<10} {sum(i.passed for i in items)}/{len(items)}")
    passed = sum(r.passed for r in results)
    lat = quantiles([r.latency_s for r in results])
    cost = quantiles([r.cost_rub for r in results])
    print(f"\nИтого: {passed}/{len(results)} ({passed / len(results):.0%})")
    print(f"Латентность, с: p50={lat['p50']:.2f} p95={lat['p95']:.2f}")
    print(f"Стоимость, ₽:   p50={cost['p50']:.3f} p95={cost['p95']:.3f}")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-k", nargs="*", default=[], help="run only cases whose id starts with these prefixes"
    )
    parser.add_argument("--repeat", type=int, default=1, help="run each case N times to measure stability")
    parser.add_argument("--temperature", type=float, help="override LLM_TEMPERATURE")
    parser.add_argument("--model", help="override the model of the active LLM_PROVIDER")
    parser.add_argument("--concurrency", type=int, default=4, help="parallel cases (mind API rate limits)")
    args = parser.parse_args()

    overrides: dict[str, Any] = {}
    if args.temperature is not None:
        overrides["llm_temperature"] = args.temperature
    settings = get_settings().model_copy(update=overrides)
    if args.model:
        settings = settings.with_llm_model(args.model)

    cases = [c for c in CASES if not args.k or any(c.id.startswith(p) for p in args.k)]
    if not cases:
        print("Нет кейсов под фильтр", file=sys.stderr)
        return 2

    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded(case: Case) -> CaseResult:
        async with semaphore:
            return await _run_safe(case, settings)

    results = await asyncio.gather(*(bounded(c) for c in cases for _ in range(args.repeat)))
    _print_report(results)

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "temperature": settings.llm_temperature,
        "repeat": args.repeat,
        "results": [asdict(r) for r in results],
    }
    model_slug = settings.llm_model.replace("/", "_")
    path = REPORTS_DIR / f"{stamp}-{model_slug}-t{settings.llm_temperature}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nОтчёт: {path}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
