"""Secret redaction: keeps API keys and passwords out of what RECAP stores and shows.

recap/runtime.py builds one `Redactor`, and it is applied at three points:

  trace payloads    the Tracer redacts every event payload before any sink (JSONL, SQLite,
                    live stream) sees it;
  tool outputs      the tool gateway redacts each tool result before it becomes evidence,
                    so checkpoint fields built from evidence inherit the redaction;
  run error text    the controller redacts a failed run's error message before saving it.

The Redactor returns a redacted deep copy and applies three rules:

  1. Sensitive keys   a dict entry whose key looks like api_key, token, secret, password,
                      authorization or credential has its value replaced by "[REDACTED]".
                      Numbers, booleans and None stay visible: under such keys they are
                      counts such as "tokens_before", not credentials.
  2. Known secrets    values of environment variables whose NAME contains KEY, SECRET,
                      TOKEN or PASSWORD (plus any extra secrets passed in) are replaced
                      wherever they appear inside a string.
  3. Key formats      strings shaped like Anthropic, OpenAI, AWS or GitHub keys, or bearer
                      tokens, are replaced.

The pattern list is best-effort, not a guarantee: a secret in a format these rules do not
recognize is stored as it is. This is a local prototype, not a data-loss-prevention system.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

REDACTED = "[REDACTED]"

SENSITIVE_KEY = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|credential)")
SECRET_ENV_NAME = re.compile(r"(?i)(KEY|SECRET|TOKEN|PASSWORD)")
MIN_ENV_SECRET_LENGTH = 8  # shorter values such as "1" or "true" would redact ordinary text

KEY_VALUE_SECRET = re.compile(
    r"(?i)\b((?:password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;'\"]+"
)

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),  # Anthropic API key
    re.compile(r"sk-[A-Za-z0-9]{16,}"),  # OpenAI-style API key
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key ID
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),  # GitHub personal access token
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{8,}"),  # HTTP "Authorization: Bearer ..." value
)


class Redactor:
    """Callable that returns a copy of a JSON-like value with secrets replaced by [REDACTED]."""

    def __init__(
        self, extra_secrets: Iterable[str] = (), environ: Mapping[str, str] | None = None
    ) -> None:
        environ = os.environ if environ is None else environ
        from_env = {
            value
            for name, value in environ.items()
            if SECRET_ENV_NAME.search(name) and len(value) >= MIN_ENV_SECRET_LENGTH
        }
        secrets = from_env | {secret for secret in extra_secrets if secret}
        # Longest first, so a secret that contains a shorter one is replaced as a whole;
        # then alphabetical, so the output never depends on set ordering.
        self._secrets = sorted(secrets, key=lambda secret: (-len(secret), secret))

    def __call__(self, value: Any) -> Any:
        """Redacted copy of dicts, lists, tuples, strings, numbers or None; never mutates input."""
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, Mapping):
            return {key: self._redact_entry(key, item) for key, item in value.items()}
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self(item) for item in value)
        if isinstance(value, BaseModel):
            # A model inside a payload is redacted as the JSON data it will be stored as.
            return self(value.model_dump(mode="json"))
        return value  # numbers, booleans, None, datetimes: nothing to hide

    def _redact_entry(self, key: Any, value: Any) -> Any:
        """Hide the value of a sensitive key unless it is a number, a flag or None."""
        if SENSITIVE_KEY.search(str(key)) and not _is_count_or_flag(value):
            return REDACTED
        return self(value)

    def _redact_text(self, text: str) -> str:
        """Replace known secret values first, then anything shaped like a credential."""
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern in SECRET_PATTERNS:
            text = pattern.sub(REDACTED, text)
        # "password=hunter2" in free text: keep the key so the line still reads sensibly.
        return KEY_VALUE_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", text)


def _is_count_or_flag(value: Any) -> bool:
    """True for values that cannot carry a credential: numbers, booleans and None."""
    return value is None or isinstance(value, int | float)
