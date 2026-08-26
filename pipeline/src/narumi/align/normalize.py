"""Text normalization used for cross-source anchor matching."""

from __future__ import annotations

import unicodedata

_DROP_CATEGORY_PREFIXES = ("P", "Z", "C")
"""Unicode categories removed by :func:`normalize_text`: punctuation, separators, controls."""


def normalize_text(text: str) -> str:
    """NFKC-normalize, lowercase and strip whitespace / punctuation / control characters.

    The result is a compact string suitable for character n-gram comparison across sources that
    may differ in spacing, width (全角 / 半角) and punctuation.
    """
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(
        ch for ch in normalized if not unicodedata.category(ch).startswith(_DROP_CATEGORY_PREFIXES)
    )


def char_ngrams(text: str, n: int) -> list[str]:
    """Return every character n-gram of ``text`` in positional order (empty if too short)."""
    if n <= 0:
        raise ValueError("n must be positive")
    if len(text) < n:
        return []
    return [text[i : i + n] for i in range(len(text) - n + 1)]
