"""Make an LLM answer safe for text-to-speech.

The prompt asks for plain text, but models still slip into markdown on long
lists. Cleaning it here keeps the spoken answer correct for any model.
"""

import re

_BULLET_RE = re.compile(r"^\s*(?:[-*•—–]|\d+[.)])\s+", re.MULTILINE)
_HEADING_RE = re.compile(r"^\s*#+\s*", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"\*\*|__|\*|`")
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-➿️]")
_SPACES_RE = re.compile(r"[ \t]{2,}")


def to_speech_text(text: str) -> str:
    text = _HEADING_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _EMPHASIS_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Join list items into one sentence-like flow: "Сегодня: созвон в 16:00; оплатить интернет."
    joined = ""
    for line in lines:
        if not joined:
            joined = line
        elif joined.endswith((":", ".", "!", "?", ";", ",")):
            joined += " " + line
        else:
            joined += "; " + line
    return _SPACES_RE.sub(" ", joined).strip()
