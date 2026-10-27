import unittest
from typing import Any

import httpx
from prometheus_client import REGISTRY

from app.mcp_server.__main__ import McpSettings, build_app
from app.mcp_server.metrics import RECORD_OPERATIONS, measured
from app.mcp_server.tools import EventTools


def count(operation: str, transport: str, outcome: str) -> float:
    labels = {"operation": operation, "transport": transport, "outcome": outcome}
    return REGISTRY.get_sample_value("assistant_record_operations_total", labels) or 0.0


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_outcomes_and_durations_are_recorded(self) -> None:
        async def ok() -> dict[str, Any]:
            return {"events": []}

        async def unavailable() -> dict[str, Any]:
            return {"error": {"code": "temporary_unavailable", "message": "", "field": None}}

        async def crash() -> dict[str, Any]:
            raise RuntimeError("unexpected")

        before = [
            count("test_op", "mcp", outcome) for outcome in ("ok", "temporary_unavailable", "exception")
        ]
        await measured("test_op", "mcp", ok)
        await measured("test_op", "mcp", unavailable)
        with self.assertRaises(RuntimeError):
            await measured("test_op", "mcp", crash)
        after = [count("test_op", "mcp", outcome) for outcome in ("ok", "temporary_unavailable", "exception")]
        self.assertEqual([a - b for a, b in zip(after, before, strict=True)], [1, 1, 1])
        observed = REGISTRY.get_sample_value(
            "assistant_record_operation_seconds_count", {"operation": "test_op", "transport": "mcp"}
        )
        self.assertGreaterEqual(observed or 0, 3)

    def test_labels_never_identify_users_or_records(self) -> None:
        self.assertEqual(RECORD_OPERATIONS._labelnames, ("operation", "transport", "outcome"))

    async def test_process_exposes_metrics_endpoint(self) -> None:
        app = build_app(EventTools(None, None, None, None), McpSettings(service_token="t" * 32))  # type: ignore[arg-type]
        await measured("test_op", "rest", self._ok)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/metrics/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("assistant_record_operations_total", response.text)

    @staticmethod
    async def _ok() -> dict[str, Any]:
        return {"events": []}
