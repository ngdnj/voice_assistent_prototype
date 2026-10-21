"""Local entry point: python -m app check|example. No MAX/AI calls."""

import argparse
import asyncio
import logging
from uuid import UUID, uuid4

from redis.exceptions import RedisError

from app.planner.repository import TaskStorageError
from app.runtime import Backend, RuntimeSettings, log_event, run_backend


def main() -> int:
    parser = argparse.ArgumentParser(description="Local backend: Redis check or task create/read example")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate settings and connect to Redis")
    example = commands.add_parser("example", help="persist and read a fixed demonstration task")
    example.add_argument("--operation-id", type=UUID, required=True, help="reuse this UUID on retry")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    request_id = uuid4()
    operation_id = args.operation_id if args.command == "example" else None
    try:
        settings = RuntimeSettings.from_env(require_demo_owner=args.command == "example")
    except ValueError:
        log_event("invalid_settings", request_id=request_id, operation_id=operation_id)
        return 2

    async def work(backend: Backend) -> None:
        if args.command == "check":
            return  # The lifecycle has already performed PING.
        if settings.demo_owner_id is None:
            raise ValueError("Missing demonstration owner")
        task = await backend.tasks.create_task(
            owner_id=settings.demo_owner_id,
            title="Локальный пример backend",
            operation_id=str(operation_id),
        )
        saved = await backend.tasks.get_task(task_id=task.id, owner_id=settings.demo_owner_id)
        if saved != task:
            raise TaskStorageError("Read-back failed")
        # Output is a CLI result, not a log of user content.
        print(f"task_id={task.id}", flush=True)

    try:
        return asyncio.run(run_backend(settings, work, request_id=request_id, operation_id=operation_id))
    except (RedisError, TaskStorageError):
        log_event("storage_unavailable", request_id=request_id, operation_id=operation_id)
        return 1
    except ValueError:
        log_event("operation_rejected", request_id=request_id, operation_id=operation_id)
        return 2
    except Exception:
        # No raw traceback: infrastructure exceptions may contain credentials.
        log_event("operation_failed", request_id=request_id, operation_id=operation_id)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
