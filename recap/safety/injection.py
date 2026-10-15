"""Prompt-injection detector for untrusted text (logs, runbooks, memories).

This is a simple pattern matcher, NOT production-grade injection protection. Its job
is to *flag* suspicious text so it is visibly marked as untrusted data in context
packs and the dashboard. The real protection is architectural: the safety gateway
denies dangerous actions no matter what any text says.
"""

from __future__ import annotations

import re

INJECTION_FLAG = "prompt_injection_suspected"

# Each pattern targets instruction-like phrasing, not ordinary operational words, so a
# runbook that says "namespace deletion is never automated" is not flagged.
PATTERNS: dict[str, re.Pattern[str]] = {
    "ignore-instructions": re.compile(
        r"\b(ignore|disregard|forget)\b[^.\n]{0,30}\b(previous|prior|above|all)\b[^.\n]{0,20}"
        r"\b(instructions?|rules|polic(y|ies)|guardrails?)\b",
        re.IGNORECASE,
    ),
    "system-override": re.compile(r"\b(system|admin|developer)\s+override\b", re.IGNORECASE),
    "role-hijack": re.compile(r"\byou are now\b|\bact as (an? )?(admin|root)\b", re.IGNORECASE),
    "imperative-destructive": re.compile(
        r"\b(call|run|execute|perform|invoke|use)\b[^.\n]{0,40}"
        r"\b(delete_namespace|delete the namespace|drop (the )?database|rm -rf|kubectl delete)\b",
        re.IGNORECASE,
    ),
}


def scan_text(text: str) -> list[str]:
    """Names of the injection patterns found in `text` (empty list if none)."""
    return [name for name, pattern in PATTERNS.items() if pattern.search(text or "")]


def injection_flags(text: str) -> list[str]:
    """['prompt_injection_suspected'] if `text` looks like an injection attempt, else []."""
    return [INJECTION_FLAG] if scan_text(text) else []
