"""Text chat with the assistant in the terminal, for quick manual checks.

uv run python -m assistant.demo.cli [--user demo] [--debug]
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from assistant.config import get_settings
from assistant.runtime import open_assistant


async def chat(user_id: str, debug: bool) -> None:
    settings = get_settings()
    async with open_assistant(settings) as assistant:
        print("Пишите запрос. Пустая строка или Ctrl+C — выход.")
        while True:
            try:
                text = (await asyncio.to_thread(input, "> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                break
            reply = await assistant.handle(
                user_id, text, reference_time=datetime.now(UTC), timezone=settings.timezone
            )
            if debug:
                for call in reply.tool_calls:
                    print(f"  ↳ {call.name}({json.dumps(call.args, ensure_ascii=False)})")
                tokens = f"{reply.input_tokens}+{reply.output_tokens} tok"
                print(f"  ⏱ {reply.latency_s:.1f}s, {tokens}, {reply.cost_rub:.3f}₽")
            print(reply.text)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="demo")
    parser.add_argument("--debug", action="store_true", help="show tool calls, latency and cost")
    args = parser.parse_args()
    asyncio.run(chat(args.user, args.debug))


if __name__ == "__main__":
    main()
