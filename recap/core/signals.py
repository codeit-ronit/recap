"""Signal conventions shared by the simulator, the scripted policy and the verifier.

The simulator writes log lines and events using the templates below, and the
scripted (non-LLM) policy and the evidence verifier parse them with the matching
helpers. Keeping both sides here guarantees they agree. A real environment adapter
would need its own parsers for real log formats, or an LLM provider that reads text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime

# --- Thresholds ---------------------------------------------------------------------

ERROR_RATE_THRESHOLD = 0.05  # a service with error_rate above this is "erroring"
MEMORY_PRESSURE_RATIO = 0.90  # memory at or above 90% of its limit is "under pressure"
MEMORY_RELAXED_RATIO = 0.70  # memory below 70% of its limit argues against pressure
RECENT_DEPLOY_MINUTES = 120  # deployments younger than this are "recent"
# A deployment explains errors only if the errors begin between 5 min before
# (metric resolution) and 30 min after it.
DEPLOY_ERROR_WINDOW_MINUTES = (-5, 30)

# --- Message templates (the simulator formats, the parsers below read) ---------------

RELEASE_ERROR_TEMPLATE = "{exception} (version={version})"
UPSTREAM_ERROR_TEMPLATE = "upstream {dependency} call failed: {reason}"
OOM_ERROR_MESSAGE = "java.lang.OutOfMemoryError: Java heap space"
HEAP_WARNING_TEMPLATE = "heap usage at {percent}% of container limit"

OOM_EVENT_REASONS = frozenset({"OOMKilled", "BackOff"})

# --- Parsers ---------------------------------------------------------------------------

_UPSTREAM_RE = re.compile(r"upstream (?P<dep>[a-z][a-z0-9-]*) call failed", re.IGNORECASE)
_VERSION_RE = re.compile(r"\(version=(?P<version>v\d+\.\d+\.\d+)\)")


def upstream_dependency(message: str) -> str | None:
    """Return the dependency named in an 'upstream X call failed' message, if any."""
    match = _UPSTREAM_RE.search(message)
    return match.group("dep").lower() if match else None


def version_tag(message: str) -> str | None:
    """Return the release version tagged in an error message, e.g. 'v2.4.1'."""
    match = _VERSION_RE.search(message)
    return match.group("version") if match else None


def is_oom_message(message: str) -> bool:
    """True if a log line reports an out-of-memory condition."""
    return "OutOfMemoryError" in message or "OOMKilled" in message


def error_onset(
    points: Iterable[tuple[datetime, float]], threshold: float = ERROR_RATE_THRESHOLD
) -> datetime | None:
    """First timestamp at which a series goes above `threshold` (None if it never does)."""
    for timestamp, value in sorted(points, key=lambda p: p[0]):
        if value > threshold:
            return timestamp
    return None


def as_utc(value: datetime) -> datetime:
    """Treat a timestamp without a timezone as UTC, and convert any other one to UTC.

    Comparing a naive and an aware datetime raises a TypeError, so every timestamp that
    is compared (observations, evidence, the alert time) goes through this one helper.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def minutes_between(earlier: datetime, later: datetime) -> float:
    """Signed minutes from `earlier` to `later`."""
    return (as_utc(later) - as_utc(earlier)).total_seconds() / 60.0
