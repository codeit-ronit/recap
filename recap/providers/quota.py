"""Free-tier guard: keeps a hosted provider at $0, or refuses to call it.

Before any request to a hosted free plan (Groq), these checks run in order, and any failure
raises BEFORE anything is sent:

    1. opt-in      RECAP_ALLOW_LIVE_FREE_INFERENCE=1 is set for this process
    2. allowlist   the model is exactly one of free_tier.allowed_models
    3. key         the key's environment variable is set, in plain ASCII (keys are read
                   only from the environment, never from files)
    4. free plan   the owner's dated confirmation (free_tier.attestation_env, YYYY-MM-DD, the
                   owner's local date) is at most attestation_max_age_days old, not ahead
    5. not stopped no earlier tripwire has stopped this provider's host (see below)

Checks 1-5 also guard the model-list request (metadata, not inference). Before each
INFERENCE request one more check runs, in the same IMMEDIATE transaction that reserves it:

    6. caps        the host is still not stopped, and RECAP's own per-minute, per-day and
                   per-run caps (set below the free plan's limits) have room. Two agents
                   or two processes cannot both take the last slot. A full PER-MINUTE cap is
                   a short wait (MinuteCapReached says how long; the provider paces itself);
                   a full daily or per-run cap defers the run (QuotaExhausted).

A reservation counts the prompt estimate plus a typical answer. When the server answers,
the reported token count replaces it; a request the server refused without generating
anything (401, 403, 429, 5xx) gives its tokens back.

After EVERY response that carries the header, whatever its status:

    tripwire  x-ratelimit-limit-requests must equal the free plan's daily limit for the model
              (1,000 for gpt-oss on Groq Free on 2026-10-21; a paid plan's is far higher).
              A different value, a successful response without the header, HTTP 402 or
              Groq's "blocked_api_access" means the account may not be free any more: the
              host is STOPPED and stays stopped until the owner checks the billing page and
              clears it (python scripts/providers.py clear-stop groq).

Why a tripwire and not a check up front: Groq has no API that says which plan an account
is on, and its model list carries no rate-limit headers (checked 2026-10-21). The owner's
dated confirmation is the up-front check; the header comparison catches a plan change on the
first response after it. Requests already in flight at that moment (another agent, another
process) are not stopped by it, which is why the owner's confirmation matters.

The ledger is one SQLite file per USER, outside every data folder
(Paths.usage_ledger, default ~/.local/state/recap/llm-usage.db), keyed by the provider's
HOST rather than its name in configs/providers.yaml. So a stop or a daily cap holds for
every RECAP process of this user, whatever RECAP_DATA_DIR it uses, and renaming a provider
entry cannot escape it. It also counts local requests (for the dashboard; no caps apply).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from recap.core.config import FreeTierSettings
from recap.core.db import connect
from recap.core.errors import ConfigError, FreeStatusLost, QuotaExhausted
from recap.core.ids import utcnow

OPT_IN_ENV = "RECAP_ALLOW_LIVE_FREE_INFERENCE"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,           -- the provider's host, e.g. "api.groq.com"
    model TEXT NOT NULL,
    at TEXT NOT NULL,              -- UTC ISO time the request was reserved
    status INTEGER,                -- HTTP status; NULL while the request is in flight
    tokens INTEGER NOT NULL,       -- reserved estimate, replaced by the reported total
    input_tokens INTEGER,
    output_tokens INTEGER,
    rate_limit TEXT,               -- JSON: the response's rate-limit headers (hosted only)
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS llm_requests_by_scope ON llm_requests (scope, at);
CREATE TABLE IF NOT EXISTS llm_stops (
    scope TEXT PRIMARY KEY,
    stopped_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
"""


def ledger_scope(base_url: str) -> str:
    """What the ledger counts and stops by: the host (and port) a provider talks to."""
    return urlsplit(base_url).netloc.lower() or base_url


class MinuteCapReached(QuotaExhausted):
    """RECAP's own per-minute cap is full: room frees up in `retry_in` seconds (pace, not stop)."""

    def __init__(self, provider: str, detail: str, retry_in: float) -> None:
        super().__init__(provider, detail)
        self.retry_in = retry_in


# Statuses that mean the server generated nothing, so a reservation's tokens are released.
_NOTHING_GENERATED = {401, 403, 429}


class UsageLedger:
    """Counts LLM requests and tokens per host, and remembers hosts that were stopped."""

    def __init__(self, db_path: Path | str) -> None:
        self._conn = connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- requests -----------------------------------------------------------------------

    def reserve(
        self,
        scope: str,
        model: str,
        tokens: int,
        caps: Any | None = None,
        *,
        refuse_if_stopped: bool = False,
        now: datetime | None = None,
    ) -> int:
        """Record a request about to be sent. With `caps`, refuse it if a cap has no room;
        with `refuse_if_stopped`, refuse it if the host was stopped.

        Every check and the insert happen in one IMMEDIATE transaction, so concurrent agents
        (or another RECAP process) cannot both take the last slot, nor slip past a stop that
        another process just recorded.
        """
        now = now or utcnow()
        with self._conn:  # commits, or rolls back on an exception
            self._conn.execute("BEGIN IMMEDIATE")
            if refuse_if_stopped:
                stop = self.stopped(scope)
                if stop is not None:
                    raise ConfigError(
                        f"{scope} was stopped at {stop['stopped_at']}: {stop['reason']}"
                    )
            if caps is not None:
                self._check_caps(scope, tokens, caps, now)
            cursor = self._conn.execute(
                "INSERT INTO llm_requests (scope, model, at, tokens) VALUES (?, ?, ?, ?)",
                (scope, model, now.isoformat(), tokens),
            )
        return int(cursor.lastrowid or 0)

    def finish(
        self,
        request_id: int,
        *,
        status: int | None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        rate_limit: Mapping[str, Any] | None = None,
        note: str = "",
    ) -> None:
        """Fill in what the server answered; reported tokens replace the reserved estimate.

        No reported tokens: a refusal or server error gives the reservation back (nothing
        was generated); any other answer keeps it, to stay on the safe side.
        """
        reported = (input_tokens or 0) + (output_tokens or 0)
        released = status is not None and (status in _NOTHING_GENERATED or status >= 500)
        if reported > 0:
            tokens = reported
        elif released:
            tokens = 0
        else:
            tokens = -1  # keep the reservation
        with self._conn:
            self._conn.execute(
                "UPDATE llm_requests SET status = ?, input_tokens = ?, output_tokens = ?,"
                " tokens = CASE WHEN ? >= 0 THEN ? ELSE tokens END, rate_limit = ?, note = ?"
                " WHERE id = ?",
                (
                    status,
                    input_tokens,
                    output_tokens,
                    tokens,
                    tokens,
                    json.dumps(dict(rate_limit)) if rate_limit else None,
                    note[:300],
                    request_id,
                ),
            )

    def window(self, scope: str, since: datetime) -> tuple[int, int]:
        """(requests, tokens) for `scope` since `since`, in-flight requests included."""
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens), 0) FROM llm_requests"
            " WHERE scope = ? AND at >= ?",
            (scope, since.isoformat()),
        ).fetchone()
        return int(row[0]), int(row[1])

    def last_rate_limit(self, scope: str) -> dict[str, Any] | None:
        """The rate-limit headers of the host's most recent response, if any."""
        row = self._conn.execute(
            "SELECT rate_limit, at FROM llm_requests WHERE scope = ? AND rate_limit IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            (scope,),
        ).fetchone()
        return {**json.loads(row[0]), "at": row[1]} if row else None

    def _check_caps(self, scope: str, tokens: int, caps: Any, now: datetime) -> None:
        """Raise if this request would pass a cap: daily caps defer, minute caps pace."""
        if tokens > caps.tokens_per_minute:
            raise QuotaExhausted(
                scope, f"one request needs {tokens} tokens, above the per-minute cap"
            )
        day = self.window(scope, now - timedelta(days=1))
        for wanted, cap, what in [
            (day[0] + 1, caps.requests_per_day, "requests in the last 24 hours"),
            (day[1] + tokens, caps.tokens_per_day, "tokens in the last 24 hours"),
        ]:
            if wanted > cap:
                raise QuotaExhausted(
                    scope, f"RECAP's own cap reached: {wanted} {what} would exceed {cap}"
                )
        # The last minute, oldest first: how long until enough of it ages out?
        recent = self._conn.execute(
            "SELECT at, tokens FROM llm_requests WHERE scope = ? AND at >= ? ORDER BY at",
            (scope, (now - timedelta(minutes=1)).isoformat()),
        ).fetchall()
        count, used = len(recent), sum(int(row[1]) for row in recent)
        if count + 1 <= caps.requests_per_minute and used + tokens <= caps.tokens_per_minute:
            return
        for at, row_tokens in recent:
            count, used = count - 1, used - int(row_tokens)
            if count + 1 <= caps.requests_per_minute and used + tokens <= caps.tokens_per_minute:
                frees = datetime.fromisoformat(at) + timedelta(minutes=1)
                wait = max(0.1, (frees - now).total_seconds())
                raise MinuteCapReached(
                    scope, f"RECAP's per-minute cap is full for {wait:.0f}s", wait
                )
        raise QuotaExhausted(scope, "RECAP's per-minute cap is full")  # pragma: no cover

    # --- stops (tripwire) ------------------------------------------------------------------

    def stop(self, scope: str, reason: str, *, now: datetime | None = None) -> None:
        """Stop using `scope` until the owner clears it (the first reason is kept)."""
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO llm_stops (scope, stopped_at, reason) VALUES (?, ?, ?)",
                (scope, (now or utcnow()).isoformat(), reason[:300]),
            )

    def stopped(self, scope: str) -> dict[str, str] | None:
        row = self._conn.execute(
            "SELECT stopped_at, reason FROM llm_stops WHERE scope = ?", (scope,)
        ).fetchone()
        return {"stopped_at": row[0], "reason": row[1]} if row else None

    def clear_stop(self, scope: str) -> bool:
        """The owner checked the billing page again: allow the host again."""
        with self._conn:
            cursor = self._conn.execute("DELETE FROM llm_stops WHERE scope = ?", (scope,))
        return cursor.rowcount > 0


class FreeTierGuard:
    """The checks around every request to one hosted free provider (see the module doc)."""

    def __init__(
        self,
        provider: str,
        settings: FreeTierSettings,
        ledger: UsageLedger,
        *,
        scope: str,
        environ: Mapping[str, str] | None = None,
        today: date | None = None,
    ) -> None:
        self.provider = provider  # the name in configs/providers.yaml (for messages)
        self.scope = scope  # the host the ledger counts and stops by
        self.settings = settings
        self.ledger = ledger
        self._environ = os.environ if environ is None else environ
        self._today = today
        self.requests_this_run = 0
        self.tripped: str | None = None  # why the tripwire fired in this run, if it did

    def api_key(self) -> str:
        """The key, read from the environment at send time (never stored by RECAP).

        It must be plain printable ASCII: a key pasted with quotes or spaces would fail
        inside the HTTP client, whose error objects would then carry the header text.
        """
        key = self._environ.get(self.settings.api_key_env, "").strip()
        if any(not 33 <= ord(char) <= 126 for char in key):
            raise ConfigError(
                f"{self.provider}: {self.settings.api_key_env} contains spaces or characters "
                "that are not plain ASCII (pasted quotes?); set it again"
            )
        return key

    def preflight(self, model: str, tokens: int) -> int:
        """Checks 1-6 for an inference request; reserves it and returns its ledger ID."""
        self.check_allowed(model)
        if self.requests_this_run >= self.settings.caps.requests_per_run:
            cap = self.settings.caps.requests_per_run
            raise QuotaExhausted(self.provider, f"RECAP's cap of {cap} requests per run reached")
        try:
            request_id = self.ledger.reserve(
                self.scope, model, tokens, self.settings.caps, refuse_if_stopped=True
            )
        except QuotaExhausted as exc:
            exc.provider = self.provider
            raise
        self.requests_this_run += 1
        return request_id

    def check_allowed(self, model: str) -> None:
        """Checks 1-5: everything except the caps. Raises ConfigError with the reason."""
        if self._environ.get(OPT_IN_ENV, "").strip() != "1":
            raise ConfigError(
                f"{self.provider} is a hosted model: live calls need {OPT_IN_ENV}=1 for this "
                "process (never set it in CI)"
            )
        if model not in self.settings.allowed_models:
            raise ConfigError(
                f"{self.provider}: model {model!r} is not in the free-tier allowlist "
                f"{self.settings.allowed_models}"
            )
        if not self.api_key():
            raise ConfigError(
                f"{self.provider}: set {self.settings.api_key_env} in the environment "
                "(RECAP never reads keys from files or the repository)"
            )
        self._check_attestation()
        stop = self.ledger.stopped(self.scope)
        if stop is not None:
            raise ConfigError(
                f"{self.provider} ({self.scope}) was stopped at {stop['stopped_at']}: "
                f"{stop['reason']}. Check the account's billing page, then run: "
                f"python scripts/providers.py clear-stop {self.provider}"
            )

    def after_response(
        self,
        request_id: int,
        model: str,
        *,
        status: int | None,
        headers: Mapping[str, str],
        error_code: str = "",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Record the answer and apply the tripwire. Returns the rate-limit snapshot."""
        snapshot = rate_limit_snapshot(headers)
        self.ledger.finish(
            request_id,
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            rate_limit=snapshot,
            note=error_code,
        )
        if status == 402 or error_code == "blocked_api_access":
            self._trip(f"the provider answered HTTP {status} {error_code}".strip())
        expected = self.settings.free_daily_request_limit.get(model)
        limit = headers.get("x-ratelimit-limit-requests")
        if limit is not None:
            # Any status: an error answer (429, a schema failure) from a paid plan carries the
            # paid plan's limit too, and a schema failure may have generated billed tokens.
            if expected is None or limit.strip() != str(expected):
                self._trip(
                    f"x-ratelimit-limit-requests is {limit!r}, but the free plan's daily "
                    f"limit for {model} is {expected}"
                )
        elif status is not None and 200 <= status < 300:
            self._trip("a successful response came without x-ratelimit-limit-requests")
        return snapshot

    def _trip(self, reason: str) -> None:
        self.tripped = reason
        self.ledger.stop(self.scope, reason)
        raise FreeStatusLost(
            f"{self.provider}: free status is no longer verified ({reason}). RECAP stopped "
            "using this provider; nothing will be retried or sent elsewhere."
        )

    def _check_attestation(self) -> None:
        name = self.settings.attestation_env
        raw = self._environ.get(name, "").strip()
        if not raw:
            raise ConfigError(
                f"{self.provider}: the owner must confirm the free plan first: check the "
                f"account's billing page, then export {name}=YYYY-MM-DD (the date checked)"
            )
        try:
            checked = date.fromisoformat(raw)
        except ValueError:
            raise ConfigError(f"{name} must be a date like 2026-10-21, not {raw[:20]!r}") from None
        # The owner writes their own LOCAL date; comparing with the UTC date would refuse a
        # confirmation made this morning in a timezone ahead of UTC.
        today = self._today or date.today()
        age = (today - checked).days
        if age < 0:
            raise ConfigError(f"{name} is in the future ({raw})")
        if age > self.settings.attestation_max_age_days:
            raise ConfigError(
                f"{self.provider}: the free-plan confirmation is {age} days old (limit "
                f"{self.settings.attestation_max_age_days}); check the billing page again and "
                f"update {name}"
            )


def rate_limit_snapshot(headers: Mapping[str, str]) -> dict[str, Any]:
    """The rate-limit headers under short names (no "token" in a key, so the trace redactor
    keeps them: it hides string values under key names that look like credentials)."""
    names = {
        "x-ratelimit-limit-requests": "rpd_limit",
        "x-ratelimit-remaining-requests": "rpd_remaining",
        "x-ratelimit-reset-requests": "rpd_reset",
        "x-ratelimit-limit-tokens": "tpm_limit",
        "x-ratelimit-remaining-tokens": "tpm_remaining",
        "x-ratelimit-reset-tokens": "tpm_reset",
        "retry-after": "retry_after",
    }
    return {short: headers[name] for name, short in names.items() if name in headers}
