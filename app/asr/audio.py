"""Audio decoding shared by ASR backends: any container -> 16 kHz mono 16-bit PCM via ffmpeg.

Decoding is a blocking subprocess call; async callers run it with `asyncio.to_thread`.
The input goes through a temporary file, not a pipe: ffmpeg cannot read
mp4/m4a from a pipe when the index is at the end of the file.
"""

import os
import subprocess
import tempfile

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # mono, s16le
FFMPEG_TIMEOUT_S = 30.0


class AudioDecodeError(ValueError):
    pass


def decode_pcm16k(audio: bytes, suffix: str) -> bytes:
    """Raw PCM (s16le, 16 kHz, mono) of an audio file in any format ffmpeg understands."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(audio)
        command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path]
        command += ["-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"]
        result = subprocess.run(command, capture_output=True, timeout=FFMPEG_TIMEOUT_S, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg не найден в PATH") from exc
    finally:
        os.unlink(path)
    if result.returncode != 0 or not result.stdout:
        raise AudioDecodeError("Не удалось прочитать аудио")
    return result.stdout


def pcm_duration_s(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SECOND
