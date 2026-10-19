"""Prometheus metrics of the ML pipeline.

Cost and latency are histograms so p50 / p95 come from `histogram_quantile`
in Grafana; `quantiles` below computes the same for offline eval reports.
"""

import statistics
from collections.abc import Sequence

from prometheus_client import Counter, Histogram

LATENCY_BUCKETS = (0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60)
COST_BUCKETS_RUB = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10)

STAGE_LATENCY = Histogram(
    "assistant_stage_latency_seconds",
    "Latency of a pipeline stage",
    ["stage"],  # asr | agent
    buckets=LATENCY_BUCKETS,
)
LLM_TOKENS = Counter("assistant_llm_tokens_total", "LLM tokens used", ["kind"])  # input | output
REQUEST_COST = Histogram(
    "assistant_request_cost_rub", "LLM cost of one user request, RUB", buckets=COST_BUCKETS_RUB
)
TOOL_CALLS = Counter("assistant_tool_calls_total", "MCP tool calls made by the agent", ["tool"])
AGENT_ERRORS = Counter("assistant_agent_errors_total", "Failed agent requests", ["reason"])


def quantiles(values: Sequence[float]) -> dict[str, float]:
    """Median and 95th percentile, the two numbers cost analytics is reported in."""
    if not values:
        return {"p50": 0.0, "p95": 0.0}
    if len(values) == 1:
        return {"p50": values[0], "p95": values[0]}
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    return {"p50": statistics.median(values), "p95": cuts[94]}
