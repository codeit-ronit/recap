"""Deterministic token estimator.

RECAP does not call a tokenizer in deterministic mode, so it estimates tokens as
"about 4 characters per token". This is an *estimate* for budgeting and is always
labeled as such; it is never reported as a real API token count.
"""

from __future__ import annotations

import math

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimated token count of `text` (at least 1 for non-empty text)."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))
