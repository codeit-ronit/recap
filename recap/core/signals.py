"""Signal conventions shared by the simulator, the scripted policy and the verifier.

The simulator writes log lines and events using the templates below, and the
scripted (non-LLM) policy and the evidence verifier parse them with the matching
helpers. Keeping both sides here guarantees they agree. A real environment adapter
would need its own parsers for real log formats, or an LLM provider that reads text.

Since M3 the parsers also read a small family of other common phrasings (an RPC client's
"rpc to X failed", "request to X timed out", "error calling X"; "[build vX.Y.Z]" or
"release vX.Y.Z" version tags; "out of memory"), so a service that logs in another style
is still understood. The simulator can log in one such style (`log_style: rpc`). The
family is a fixed list, not a model: a phrasing outside it is not recognized
(tests/unit/test_signals.py pins both what is and what is not).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime

# --- Thresholds ---------------------------------------------------------------------

ERROR_RATE_THRESHOLD = 0.05  # a service with error_rate above this is "erroring"
LATENCY_SLO_MS = 500.0  # p95 latency above this is not healthy (the shop's SLO); timeouts show here
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

# The same four messages as an RPC-style service writes them (scenario `log_style: rpc`).
RPC_RELEASE_ERROR_TEMPLATE = "handler panic: {exception} [build {version}]"
RPC_UPSTREAM_ERROR_TEMPLATE = "rpc to {dependency} failed: DEADLINE_EXCEEDED ({reason})"
RPC_OOM_ERROR_MESSAGE = "fatal error: runtime: out of memory"
RPC_HEAP_WARNING_TEMPLATE = "memory pressure: {percent}% of the container limit in use"

OOM_EVENT_REASONS = frozenset({"OOMKilled", "BackOff"})

# --- Parsers ---------------------------------------------------------------------------

_NAME = r"(?P<dep>[a-z][a-z0-9-]*)"  # a service name (see SERVICE_NAME in scenario.py)
# "This service's call to another one failed", in the phrasings RECAP reads.
_UPSTREAM_RES = [
    re.compile(rf"\bupstream {_NAME} call failed", re.IGNORECASE),
    re.compile(rf"\b(?:rpc|call|request)s? to {_NAME} (?:failed|timed out)", re.IGNORECASE),
    re.compile(rf"\berror (?:calling|contacting) {_NAME}\b", re.IGNORECASE),
]
_VERSION = r"(?P<version>v\d+\.\d+\.\d+)"
_VERSION_RES = [
    re.compile(rf"\(version={_VERSION}\)"),
    re.compile(rf"\[build {_VERSION}\]"),
    re.compile(rf"\b(?:version|release|build)[ =:]{_VERSION}\b", re.IGNORECASE),
]
_OOM_RE = re.compile(r"OutOfMemoryError|OOMKilled|\bout[ -]of[ -]memory\b", re.IGNORECASE)


def upstream_dependency(message: str) -> str | None:
    """Return the dependency a failed-call message names (e.g. 'upstream X call failed')."""
    for pattern in _UPSTREAM_RES:
        match = pattern.search(message)
        if match:
            return match.group("dep").lower()
    return None


def version_tag(message: str) -> str | None:
    """Return the release version tagged in an error message, e.g. 'v2.4.1'."""
    for pattern in _VERSION_RES:
        match = pattern.search(message)
        if match:
            return match.group("version")
    return None


def is_oom_message(message: str) -> bool:
    """True if a log line reports an out-of-memory condition."""
    return bool(_OOM_RE.search(message))


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
