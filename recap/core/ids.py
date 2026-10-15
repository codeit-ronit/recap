"""Small helpers for IDs, stable hashes and the current time.

Kept in one place so every module creates IDs and digests the same way.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Random short ID such as 'run-3f9a2c1b0d'. Used for runs, proposals, approvals."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def canonical_json(value: Any) -> str:
    """JSON with sorted keys and no extra spaces, so equal data always gives equal text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(text: str) -> str:
    """SHA-256 hex digest of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_of(value: Any) -> str:
    """Stable fingerprint of any JSON-like value (used to bind approvals to one exact action)."""
    return sha256_hex(canonical_json(value))
