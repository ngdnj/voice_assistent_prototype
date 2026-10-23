"""Prometheus metrics of role 4: record operations through MCP and the Mini App REST API.

Labels are bounded enums only (operation, transport, outcome); never users, texts or IDs (AGENTS §15).
Storage outages show up as outcome="temporary_unavailable".
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from prometheus_client import Counter, Histogram

Transport = Literal["mcp", "rest"]
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5)

RECORD_OPERATIONS = Counter(
    "assistant_record_operations_total",
    "Record operations by outcome: ok or a structured error code",
    ["operation", "transport", "outcome"],
)
RECORD_OPERATION_SECONDS = Histogram(
    "assistant_record_operation_seconds",
    "Duration of a record operation including PostgreSQL",
    ["operation", "transport"],
    buckets=DURATION_BUCKETS,
)


async def measured(
    operation: str, transport: Transport, call: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    """Run one operation and record its outcome and duration; unexpected errors are re-raised."""
    started = time.perf_counter()
    outcome = "exception"
    try:
        result = await call()
        outcome = result["error"]["code"] if "error" in result else "ok"
        return result
    finally:
        RECORD_OPERATION_SECONDS.labels(operation, transport).observe(time.perf_counter() - started)
        RECORD_OPERATIONS.labels(operation, transport, outcome).inc()
