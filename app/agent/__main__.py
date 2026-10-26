"""One local text turn against GigaChat and the running MCP/PostgreSQL backend."""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from uuid import UUID, uuid4

from app.agent.agent import Assistant, Pricing
from app.agent.history import RedisChatHistory
from app.agent.mcp import McpGateway
from app.config import get_settings
from app.llm.model import build_chat_model
from app.runtime import Backend, RuntimeSettings, log_event, run_backend


async def run(args: argparse.Namespace, text: str) -> int:
    settings = get_settings()
    model = build_chat_model(settings)
    gateway = McpGateway(settings.mcp_url or "", os.environ.get("MCP_SERVICE_TOKEN", ""))
    request_id = args.request_id
    runtime = RuntimeSettings.from_env()

    async def work(backend: Backend) -> None:
        # Reuse the lifecycle's existing Redis pool; planner operations go through MCP.
        history = RedisChatHistory(
            backend.redis,
            settings.history_max_turns,
            settings.history_ttl_s,
            settings.history_max_tokens,
        )
        assistant = Assistant(model, gateway, history, Pricing(*settings.pricing))
        reply = await assistant.handle(
            args.owner_id,
            text,
            reference_time=args.reference_time,
            timezone=args.timezone,
            chat_id=args.chat_id,
            operation_id=str(args.operation_id),
            request_id=str(request_id),
        )
        print(reply.text, flush=True)

    return await run_backend(runtime, work, request_id=request_id, operation_id=args.operation_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner-id", required=True, help="trusted local test identity, not MAX authentication"
    )
    parser.add_argument("--chat-id", default="local")
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--operation-id", type=UUID, required=True, help="reuse on retry of this input")
    parser.add_argument("--reference-time", type=datetime.fromisoformat, required=True, help="aware ISO 8601")
    args = parser.parse_args()
    args.request_id = uuid4()
    text = sys.stdin.read().strip()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("it_gorky").setLevel(logging.INFO)
    try:
        return asyncio.run(run(args, text))
    except Exception:
        # Provider/MCP failures may embed input, credentials, URLs or request bodies.
        log_event("text_agent_failed", request_id=args.request_id, operation_id=args.operation_id)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
