"""ASR quality metrics: WER, CER and correctness of numbers (times, dates, sums).

Both texts are normalized first, so the score measures recognition, not formatting:
case, punctuation, «ё», and «9:30» vs «9 30» vs «09:30» do not count as errors.
Number grouping is not normalized: «2500» and «2 500» differ, because merging
digit groups would also merge «в 9 30» into «930».
"""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

_TIME_SEPARATOR_RE = re.compile(r"(?<=\d)[:.](?=\d)")
_NON_WORD_RE = re.compile(r"[^\w\s]|_")


def normalize(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    text = _TIME_SEPARATOR_RE.sub(" ", text)
    text = _NON_WORD_RE.sub(" ", text)
    return [(token.lstrip("0") or "0") if token.isdigit() else token for token in text.split()]


def edit_distance(ref: Sequence[str], hyp: Sequence[str]) -> int:
    """Levenshtein distance: substitutions + deletions + insertions."""
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        current = [i]
        for j, h in enumerate(hyp, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h)))
        previous = current
    return previous[-1]


@dataclass(frozen=True, slots=True)
class Score:
    word_errors: int
    ref_words: int
    char_errors: int
    ref_chars: int
    exact: bool
    numbers_ok: bool  # every number of the reference recognized, and no extra ones

    @property
    def wer(self) -> float:
        return self.word_errors / max(self.ref_words, 1)

    @property
    def cer(self) -> float:
        return self.char_errors / max(self.ref_chars, 1)


def score(reference: str, hypothesis: str) -> Score:
    ref, hyp = normalize(reference), normalize(hypothesis)
    ref_chars, hyp_chars = " ".join(ref), " ".join(hyp)
    numbers = Counter(t for t in ref if t.isdigit()), Counter(t for t in hyp if t.isdigit())
    return Score(
        word_errors=edit_distance(ref, hyp),
        ref_words=len(ref),
        char_errors=edit_distance(ref_chars, hyp_chars),
        ref_chars=len(ref_chars),
        exact=ref == hyp,
        numbers_ok=numbers[0] == numbers[1],
    )


def corpus_rates(scores: Sequence[Score]) -> dict[str, float]:
    """Corpus-level WER/CER (total errors / total length), not a mean of per-phrase rates."""
    n = max(len(scores), 1)
    return {
        "wer": sum(s.word_errors for s in scores) / max(sum(s.ref_words for s in scores), 1),
        "cer": sum(s.char_errors for s in scores) / max(sum(s.ref_chars for s in scores), 1),
        "exact": sum(s.exact for s in scores) / n,
        "numbers_ok": sum(s.numbers_ok for s in scores) / n,
    }
