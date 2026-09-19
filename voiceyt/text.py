"""Transcript normalization and phrase matching helpers (plan2.md §6.1).

ASR output is inconsistent: the trigger comes back as ``youtube``, ``you tube``,
``iutube``, ``u tube``, ``youtube,`` and worse.  Everything is normalized first,
then matched as whole words so that ``youtubers`` never triggers anything.

Normalization order is fixed by the plan:
    1. lower-case
    2. strip accents (``para`` must match ``pára``)
    3. strip punctuation
    4. collapse whitespace
"""

from __future__ import annotations

import re
import unicodedata

# Keep letters, digits and underscore; everything else is a separator.
_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

_phrase_cache: dict[str, tuple[str, ...]] = {}


def strip_accents(text: str) -> str:
    """Return *text* with combining marks removed (próximo -> proximo)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Lower-case, de-accent, de-punctuate and whitespace-collapse *text*."""
    if not text:
        return ""
    lowered = strip_accents(text).lower()
    without_punct = _PUNCTUATION.sub(" ", lowered)
    return _WHITESPACE.sub(" ", without_punct).strip()


def tokenize(text: str) -> list[str]:
    """Tokenize already-normalized text (see :func:`phrase_tokens`)."""
    return text.split()


def phrase_tokens(phrase: str) -> tuple[str, ...]:
    """Normalize a configured phrase and return its token tuple (cached)."""
    cached = _phrase_cache.get(phrase)
    if cached is None:
        cached = tuple(tokenize(normalize(phrase)))
        _phrase_cache[phrase] = cached
    return cached


def match_at(tokens: list[str], phrase: tuple[str, ...], start: int) -> bool:
    """True when *phrase* occurs in *tokens* at position *start*."""
    if not phrase or start + len(phrase) > len(tokens):
        return False
    return tuple(tokens[start : start + len(phrase)]) == phrase


def find_phrase(
    tokens: list[str], phrases: list[tuple[str, ...]], start: int = 0
) -> tuple[int, int] | None:
    """Find the longest phrase from *phrases* at or after *start*.

    Returns ``(index, length_in_tokens)`` or ``None``.  Longest-first ordering is
    what makes multi-word verbs like ``mais alto`` win over the bare ``mais``.
    """
    best: tuple[int, int] | None = None
    best_len = 0
    for index in range(start, len(tokens)):
        for phrase in phrases:
            if len(phrase) >= best_len and match_at(tokens, phrase, index):
                best = (index, len(phrase))
                best_len = len(phrase)
        if best is not None and best[0] == index:
            # Longest phrase at the earliest position wins immediately.
            return best
    return best


def sort_longest_first(phrases: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Order phrases by token count, longest first, then alphabetically."""
    return sorted(phrases, key=lambda p: (-len(p), p))