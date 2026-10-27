"""Queue test of the local ASR: N voice messages arrive at once, how long does each one wait?

    uv run --extra asr python -m evals.asr_load                       # 1, 5, 10, 20 at once
    uv run --extra asr python -m evals.asr_load --threads 2           # roughly a 2 vCPU VM
    uv run --extra asr python -m evals.asr_load --concurrency 10 30
    uv run --extra asr python -m evals.asr_load --threads 2 --overflow   # GigaAM + SpeechKit at peaks, paid

GigaAM recognizes one message at a time (the adapter holds a lock), so the others queue up.
Uses the recordings in data/asr/ (see evals/asr_run.py); free, nothing leaves the machine.
`--threads` limits torch CPU threads: an approximation of a smaller VM, not a measurement on it.
A JSON report is written to evals/reports/asr-load-*.json.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.asr.base import SpeechRecognizer
from app.asr.factory import build_recognizer
from app.asr.gigaam_asr import GigaAMRecognizer
from app.asr.overflow import OverflowRecognizer
from app.config import get_settings
from evals.asr_run import AUDIO_SUFFIXES
from evals.stats import describe

REPORTS_DIR = Path(__file__).parent / "reports"


def _audio_files(data: Path) -> list[Path]:
    return sorted(p for p in data.iterdir() if p.suffix in AUDIO_SUFFIXES)


async def _burst(recognizer: SpeechRecognizer, files: list[Path], n: int) -> dict[str, Any]:
    """`n` messages at the same moment; time per message from arrival to its transcript."""
    # Read before the burst: a message arrives already downloaded, as from the bot.
    batch = [(p.read_bytes(), p.suffix) for p in (files[i % len(files)] for i in range(n))]

    async def one(audio: bytes, suffix: str) -> tuple[float, float]:
        arrived = time.perf_counter()
        transcript = await recognizer.transcribe(audio, suffix)
        return time.perf_counter() - arrived, transcript.audio_s

    started = time.perf_counter()
    results = await asyncio.gather(*(one(audio, suffix) for audio, suffix in batch))
    wall = time.perf_counter() - started
    waits = [r[0] for r in results]
    return {
        "concurrency": n,
        "time_to_text_s": describe(waits),
        "max_s": max(waits),
        "per_minute": n / wall * 60,
        "audio_s_mean": statistics.fmean(r[1] for r in results),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data", type=Path, default=Path("data/asr"))
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 5, 10, 20])
    parser.add_argument("--threads", type=int, help="torch CPU threads (default: all cores)")
    parser.add_argument(
        "--overflow",
        action="store_true",
        help="ASR_OVERFLOW=yandex: messages above ASR_OVERFLOW_QUEUE go to SpeechKit (paid, 0.16 RUB each)",
    )
    args = parser.parse_args()

    files = _audio_files(args.data) if args.data.is_dir() else []
    if not files:
        print(f"Нет записей в {args.data}: см. python -m evals.asr_record", file=sys.stderr)
        return 2
    if args.threads:
        import torch

        torch.set_num_threads(args.threads)

    settings = get_settings()
    local = GigaAMRecognizer(settings.gigaam_model)
    await asyncio.to_thread(local.warm_up)
    recognizer: SpeechRecognizer = local
    if args.overflow:
        # The same wrapper as ASR_OVERFLOW=yandex in app/asr/factory.py.
        cloud = build_recognizer(settings.model_copy(update={"asr_backend": "yandex"}))
        recognizer = OverflowRecognizer(local, cloud, max_in_primary=settings.asr_overflow_queue)
    await _burst(recognizer, files, 1)  # the first call is slower

    rows = []
    print("| Одновременно | До текста, с: p50 / p95 / max | Голосовых в минуту |")
    print("|---|---|---|")
    for n in args.concurrency:
        row = await _burst(recognizer, files, n)
        rows.append(row)
        t = row["time_to_text_s"]
        print(f"| {n} | {t['median']:.1f} / {t['p95']:.1f} / {row['max_s']:.1f} | {row['per_minute']:.0f} |")
    await recognizer.aclose()

    REPORTS_DIR.mkdir(exist_ok=True)
    report = {
        "backend": "gigaam+yandex" if args.overflow else "gigaam",
        "overflow_queue": settings.asr_overflow_queue if args.overflow else None,
        "threads": args.threads or os.cpu_count(),
        "cpu_count": os.cpu_count(),
        "files": len(files),
        "bursts": rows,
    }
    suffix = "-overflow" if args.overflow else ""
    path = REPORTS_DIR / f"asr-load-{datetime.now():%Y%m%d-%H%M%S}-t{report['threads']}{suffix}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nОтчёт: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
