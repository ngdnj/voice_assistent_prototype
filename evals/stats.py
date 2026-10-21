"""Summary statistics of eval results, shared by the runner and the model table (AGENTS §15)."""

import statistics
from collections.abc import Iterable, Sequence
from typing import Any

from app.metrics import quantiles


def describe(values: Sequence[float]) -> dict[str, float]:
    """Mean, median and p95: the numbers latency and cost are reported in."""
    if not values:
        return {"mean": 0.0, "median": 0.0, "p95": 0.0}
    q = quantiles(values)
    return {"mean": statistics.fmean(values), "median": q["p50"], "p95": q["p95"]}


def summarize(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Accuracy and per-request stats over scored results (API failures are counted separately)."""
    items = list(results)
    scored = [r for r in items if not r["infra_error"]]
    passed = sum(r["passed"] for r in scored)
    by_category: dict[str, list[int]] = {}
    for r in scored:
        counts = by_category.setdefault(r["category"], [0, 0])
        counts[0] += r["passed"]
        counts[1] += 1
    return {
        "n": len(scored),
        "passed": passed,
        "accuracy": passed / len(scored) if scored else 0.0,
        "infra_errors": len(items) - len(scored),
        "by_category": {c: {"passed": p, "n": n} for c, (p, n) in sorted(by_category.items())},
        "latency_s": describe([r["latency_s"] for r in scored]),
        "cost_rub": describe([r["cost_rub"] for r in scored]),
        "input_tokens": describe([r["input_tokens"] for r in scored]),
        "output_tokens": describe([r["output_tokens"] for r in scored]),
    }
