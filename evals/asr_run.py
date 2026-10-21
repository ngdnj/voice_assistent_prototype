"""Compare ASR backends on recorded Russian phrases: WER/CER, numbers, latency, CPU/RAM, cost.

    uv run --extra asr python -m evals.asr_run                        # all configured backends
    uv run --extra asr python -m evals.asr_run --backend gigaam --limit 5

Phrases and references are in evals/asr_phrases.tsv. Audio lives outside git in
data/asr/<id>.<wav|ogg|m4a|webm|mp3> (record it with `python -m evals.asr_record`).
If a speaker said a phrase differently, put the actual words in data/asr/refs.tsv
(`id<TAB>text`): the reference must be what was said, not what was meant.

Each backend first transcribes one file that is not scored (model load / TLS handshake).
CPU and RAM are measured for the current process, so they are meaningful only for
local backends (GigaAM); a cloud backend uses almost no local CPU by design.
A JSON report is written to evals/reports/asr-*.json.
"""

import argparse
import asyncio
import csv
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from app.asr.base import SpeechRecognizer
from app.asr.factory import build_recognizer
from app.config import Settings, get_settings
from app.metrics import quantiles
from evals.asr_metrics import Score, corpus_rates, score

PHRASES_FILE = Path(__file__).parent / "asr_phrases.tsv"
REPORTS_DIR = Path(__file__).parent / "reports"
AUDIO_SUFFIXES = (".wav", ".ogg", ".opus", ".m4a", ".webm", ".mp3", ".flac")
# Yandex SpeechKit price with VAT per started 15 s of audio, checked 2026-10-21:
# https://aistudio.yandex.ru/docs/ru/speechkit/pricing
YANDEX_RUB_PER_15S = 0.1626


@dataclass(frozen=True, slots=True)
class Phrase:
    id: str
    condition: str  # quiet | noise
    tags: tuple[str, ...]
    text: str
    audio: Path


@dataclass(slots=True)
class PhraseResult:
    id: str
    condition: str
    tags: tuple[str, ...]
    reference: str
    hypothesis: str
    wer: float
    cer: float
    numbers_ok: bool
    latency_s: float
    audio_s: float
    cpu_s: float
    error: str | None = None


def load_phrases(data_dir: Path) -> tuple[list[Phrase], list[str]]:
    """Phrases that have audio, and ids of those that are not recorded yet."""
    overrides: dict[str, str] = {}
    refs_file = data_dir / "refs.tsv"
    if refs_file.exists():
        with refs_file.open(encoding="utf-8") as file:
            overrides = {row[0]: row[1] for row in csv.reader(file, delimiter="\t") if len(row) >= 2}
    phrases, missing = [], []
    with PHRASES_FILE.open(encoding="utf-8") as file:
        for row in csv.DictReader(file, delimiter="\t"):
            audio = next((p for s in AUDIO_SUFFIXES if (p := data_dir / f"{row['id']}{s}").exists()), None)
            if audio is None:
                missing.append(row["id"])
                continue
            text = overrides.get(row["id"], row["text"])
            tags = tuple(t for t in row["tags"].split(",") if t)
            phrases.append(Phrase(row["id"], row["condition"], tags, text, audio))
    return phrases, missing


def _cpu_s(process: psutil.Process) -> float:
    times = process.cpu_times()
    return float(times.user + times.system)


def _peak_rss_mb(process: psutil.Process) -> float:
    info = process.memory_info()
    # Windows reports the true peak; elsewhere the current RSS sampled after each call.
    return float(getattr(info, "peak_wset", info.rss)) / 2**20


async def run_backend(backend: str, settings: Settings, phrases: list[Phrase]) -> dict[str, Any]:
    process = psutil.Process()
    rss_before = process.memory_info().rss / 2**20
    recognizer: SpeechRecognizer = build_recognizer(settings.model_copy(update={"asr_backend": backend}))
    results: list[PhraseResult] = []
    peak_rss = rss_before
    try:
        started = time.perf_counter()
        warm = phrases[0]
        await recognizer.transcribe(warm.audio.read_bytes(), warm.audio.suffix)
        warm_up_s = time.perf_counter() - started
        rss_loaded = process.memory_info().rss / 2**20
        for phrase in phrases:
            results.append(await _run_phrase(recognizer, phrase, process))
            peak_rss = max(peak_rss, _peak_rss_mb(process))
            print(f"  {backend} {phrase.id}: {results[-1].hypothesis!r}", file=sys.stderr)
    finally:
        await recognizer.aclose()
    return {
        "backend": backend,
        "warm_up_s": round(warm_up_s, 2),
        "rss_mb": {"before": round(rss_before), "loaded": round(rss_loaded), "peak": round(peak_rss)},
        "summary": summarize(backend, results),
        "by_condition": {
            c: summarize(backend, [r for r in results if r.condition == c])
            for c in sorted({r.condition for r in results})
        },
        "phrases": [asdict(r) for r in results],
    }


async def _run_phrase(recognizer: SpeechRecognizer, phrase: Phrase, process: psutil.Process) -> PhraseResult:
    cpu_before = _cpu_s(process)
    error: str | None
    try:
        transcript = await recognizer.transcribe(phrase.audio.read_bytes(), phrase.audio.suffix)
    except Exception as exc:  # one failed file must not stop the comparison; it is counted
        hypothesis, latency, audio_s, error = "", 0.0, 0.0, f"{type(exc).__name__}: {exc}"
    else:
        hypothesis, latency, audio_s = transcript.text, transcript.latency_s, transcript.audio_s
        error = None
    s: Score = score(phrase.text, hypothesis)
    return PhraseResult(
        id=phrase.id,
        condition=phrase.condition,
        tags=phrase.tags,
        reference=phrase.text,
        hypothesis=hypothesis,
        wer=round(s.wer, 3),
        cer=round(s.cer, 3),
        numbers_ok=s.numbers_ok,
        latency_s=round(latency, 3),
        audio_s=round(audio_s, 2),
        cpu_s=round(_cpu_s(process) - cpu_before, 3),
        error=error,
    )


def summarize(backend: str, results: list[PhraseResult]) -> dict[str, Any]:
    ok = [r for r in results if r.error is None]
    scores = [score(r.reference, r.hypothesis) for r in results]  # failed files count as all-wrong
    latencies = [r.latency_s for r in ok]
    audio_total = sum(r.audio_s for r in ok)
    summary: dict[str, Any] = {
        "n": len(results),
        "errors": len(results) - len(ok),
        **{k: round(v, 4) for k, v in corpus_rates(scores).items()},
        "latency_s": {
            "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
            **{k: round(v, 3) for k, v in quantiles(latencies).items()},
        },
        "audio_s_total": round(audio_total, 1),
        # Real-time factor: processing time per second of audio.
        "rtf": round(sum(latencies) / audio_total, 3) if audio_total else 0.0,
        "cpu_s_per_audio_s": round(sum(r.cpu_s for r in ok) / audio_total, 3) if audio_total else 0.0,
    }
    if backend == "yandex":
        units = sum(math.ceil(r.audio_s / 15) for r in ok)
        summary["cost_rub"] = {
            "total": round(units * YANDEX_RUB_PER_15S, 2),
            "per_phrase": YANDEX_RUB_PER_15S,
        }
    return summary


def print_table(reports: list[dict[str, Any]]) -> None:
    print("| ASR | Условия | N | WER | CER | Точно | Числа | p50 / p95, с | RTF | CPU-с / аудио-с |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for report in reports:
        rows = {"все": report["summary"], **report["by_condition"]}
        for condition, s in rows.items():
            print(
                f"| {report['backend']} | {condition} | {s['n']} | {s['wer']:.1%} | {s['cer']:.1%} "
                f"| {s['exact']:.0%} | {s['numbers_ok']:.0%} "
                f"| {s['latency_s']['p50']:.2f} / {s['latency_s']['p95']:.2f} | {s['rtf']:.2f} "
                f"| {s['cpu_s_per_audio_s']:.2f} |"
            )


def configured_backends(settings: Settings) -> list[str]:
    return ["gigaam", *(["yandex"] if settings.yandex_api_key else [])]


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--backend", nargs="+", choices=["gigaam", "yandex"])
    parser.add_argument("--data", type=Path, default=Path("data/asr"))
    parser.add_argument("--limit", type=int, help="only the first N recorded phrases")
    args = parser.parse_args()

    settings = get_settings()
    phrases, missing = load_phrases(args.data)
    if missing:
        print(f"Нет записи для {len(missing)} фраз: {', '.join(missing)}", file=sys.stderr)
    phrases = phrases[: args.limit] if args.limit else phrases
    if not phrases:
        print(f"Нет аудио в {args.data}. Запишите фразы: python -m evals.asr_record", file=sys.stderr)
        return 1

    reports = []
    for backend in args.backend or configured_backends(settings):
        # Backends run one after another in the same process: GigaAM CPU/RAM are not mixed with
        # anything else, and a cloud run does not compete with the local model for CPU.
        reports.append(await run_backend(backend, settings, phrases))
    print_table(reports)

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"asr-{stamp}.json"
    path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nОтчёт: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
