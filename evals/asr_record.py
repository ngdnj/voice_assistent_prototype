"""Record the ASR eval phrases from a microphone into data/asr/<id>.wav (outside git).

    python -m evals.asr_record --list-devices
    python -m evals.asr_record                         # first microphone, phrases not recorded yet
    python -m evals.asr_record --device "Микрофон (Realtek Audio)" --redo 07 12

For each phrase: Enter starts recording, Enter stops it. Say the phrase naturally
(«в девять тридцать» for 9:30). Phrases with condition "noise" are recorded with
background noise: music, TV, street, a running tap. Uses ffmpeg with DirectShow,
so it works on Windows; elsewhere pass --input-format pulse/avfoundation.
"""

import argparse
import csv
import re
import subprocess
import sys
import time
from pathlib import Path

from evals.asr_run import PHRASES_FILE

_DSHOW_AUDIO_RE = re.compile(r'"([^"]+)" \(audio\)')


def list_devices() -> list[str]:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return _DSHOW_AUDIO_RE.findall(result.stderr)


# DirectShow buffers 500 ms by default: the first buffer came out silent and the first words were lost
# (speech began at exactly 0.50 s in all 50 recordings of 26.10). A small buffer and a pause before
# «говорите» keep the start of the phrase; the leading silence does not hurt recognition.
DSHOW_BUFFER_MS = "50"
LEAD_IN_S = 1.0


def record(path: Path, input_format: str, device: str) -> None:
    source = f"audio={device}" if input_format == "dshow" else device
    buffer = ["-audio_buffer_size", DSHOW_BUFFER_MS] if input_format == "dshow" else []
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        input_format,
        *buffer,
        "-i",
        source,
    ]
    process = subprocess.Popen([*command, "-ac", "1", "-ar", "16000", str(path)], stdin=subprocess.PIPE)
    time.sleep(LEAD_IN_S)  # the device needs a moment to start delivering audio
    input("  ● говорите, Enter — стоп")
    process.communicate(b"q", timeout=10)  # "q" makes ffmpeg finish the file properly


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data", type=Path, default=Path("data/asr"))
    parser.add_argument("--input-format", default="dshow")
    parser.add_argument("--device", help="microphone name; default — the first one found")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--redo", nargs="+", default=[], help="re-record these phrase ids")
    args = parser.parse_args()

    devices = list_devices() if args.input_format == "dshow" else []
    if args.list_devices:
        print("\n".join(devices) or "Микрофоны не найдены")
        return 0
    device = args.device or (devices[0] if devices else None)
    if device is None:
        print("Микрофон не найден, укажите --device", file=sys.stderr)
        return 1
    print(f"Микрофон: {device}")

    args.data.mkdir(parents=True, exist_ok=True)
    with PHRASES_FILE.open(encoding="utf-8") as file:
        rows = list(csv.DictReader(file, delimiter="\t"))
    for row in rows:
        path = args.data / f"{row['id']}.wav"
        if path.exists() and row["id"] not in args.redo:
            continue
        noise = "  [С ШУМОМ]" if row["condition"] == "noise" else ""
        print(f"\n{row['id']}{noise}: {row['text']}")
        answer = input("  Enter — начать, s — пропустить, q — выйти: ").strip().lower()
        if answer == "q":
            break
        if answer != "s":
            record(path, args.input_format, device)
    print(f"\nГотово. Записи в {args.data}. Дальше: uv run --extra asr python -m evals.asr_run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
