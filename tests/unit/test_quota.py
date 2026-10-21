"""Tests for the usage ledger and the free-tier guard (recap/providers/quota.py)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from recap.core.config import FreeTierSettings, QuotaCaps
from recap.core.errors import ConfigError, QuotaExhausted
from recap.core.ids import utcnow
from recap.providers.quota import (
    OPT_IN_ENV,
    FreeTierGuard,
    MinuteCapReached,
    UsageLedger,
    rate_limit_snapshot,
)
from recap.telemetry.redact import Redactor

CAPS = QuotaCaps(
    requests_per_minute=2,
    requests_per_day=5,
    tokens_per_minute=1000,
    tokens_per_day=4000,
    requests_per_run=3,
)
FREE = FreeTierSettings(
    api_key_env="TEST_KEY",
    attestation_env="TEST_FREE_ON",
    allowed_models=["m-free"],
    free_daily_request_limit={"m-free": 1000},
    limits_checked_on="2026-10-21",
    caps=CAPS,
)
TODAY = date(2026, 10, 21)
OWNER = {OPT_IN_ENV: "1", "TEST_KEY": "secret-value-123", "TEST_FREE_ON": "2026-10-20"}


def ledger(tmp_path: Path) -> UsageLedger:
    return UsageLedger(tmp_path / "recap.db")


def guard(tmp_path: Path, **environ: str) -> FreeTierGuard:
    return FreeTierGuard(
        "free", FREE, ledger(tmp_path), scope="free", environ={**OWNER, **environ}, today=TODAY
    )


# --- the ledger ----------------------------------------------------------------------------


def test_reported_tokens_replace_the_reservation(tmp_path: Path) -> None:
    book = ledger(tmp_path)
    now = utcnow()
    request = book.reserve("free", "m-free", 900, now=now)
    assert book.window("free", now - timedelta(minutes=1)) == (1, 900)

    book.finish(request, status=200, input_tokens=300, output_tokens=50)

    assert book.window("free", now - timedelta(minutes=1)) == (1, 350)


@pytest.mark.parametrize(("status", "kept"), [(429, 0), (503, 0), (401, 0), (200, 900), (400, 900)])
def test_a_refused_request_gives_its_tokens_back(tmp_path: Path, status: int, kept: int) -> None:
    book = ledger(tmp_path)
    now = utcnow()
    request = book.reserve("free", "m-free", 900, now=now)
    book.finish(request, status=status)  # no usage reported
    assert book.window("free", now - timedelta(minutes=1)) == (1, kept)


def test_every_process_shares_the_same_counts(tmp_path: Path) -> None:
    now = utcnow()
    ledger(tmp_path).reserve("free", "m-free", 10, now=now)
    assert ledger(tmp_path).window("free", now - timedelta(minutes=1)) == (1, 10)


def test_a_full_minute_cap_says_when_it_frees_up(tmp_path: Path) -> None:
    book = ledger(tmp_path)
    now = utcnow()
    book.reserve("free", "m-free", 10, CAPS, now=now - timedelta(seconds=50))
    book.reserve("free", "m-free", 10, CAPS, now=now - timedelta(seconds=20))

    with pytest.raises(MinuteCapReached) as full:
        book.reserve("free", "m-free", 10, CAPS, now=now)

    assert 9 <= full.value.retry_in <= 11  # the oldest request leaves the window in ~10 s
    assert book.window("free", now - timedelta(minutes=5)) == (2, 20)  # nothing was reserved


def test_a_full_daily_cap_defers_instead_of_waiting(tmp_path: Path) -> None:
    book = ledger(tmp_path)
    now = utcnow()
    for hours in range(5, 0, -1):
        book.reserve("free", "m-free", 10, CAPS, now=now - timedelta(hours=hours))
    with pytest.raises(QuotaExhausted, match="requests in the last 24 hours") as stopped:
        book.reserve("free", "m-free", 10, CAPS, now=now)
    assert not isinstance(stopped.value, MinuteCapReached)


def test_a_request_bigger_than_the_minute_cap_is_refused_outright(tmp_path: Path) -> None:
    with pytest.raises(QuotaExhausted, match="above the per-minute cap") as refused:
        ledger(tmp_path).reserve("free", "m-free", 1001, CAPS)
    assert not isinstance(refused.value, MinuteCapReached)


def test_a_stop_is_remembered_until_cleared(tmp_path: Path) -> None:
    book = ledger(tmp_path)
    book.stop("free", "first reason")
    book.stop("free", "second reason")
    assert ledger(tmp_path).stopped("free")["reason"] == "first reason"  # type: ignore[index]
    assert book.clear_stop("free") is True
    assert book.stopped("free") is None and book.clear_stop("free") is False


# --- the guard -----------------------------------------------------------------------------


def test_a_fresh_owner_confirmation_lets_the_request_through(tmp_path: Path) -> None:
    keeper = guard(tmp_path)
    request_id = keeper.preflight("m-free", 100)
    assert request_id > 0 and keeper.requests_this_run == 1


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({OPT_IN_ENV: "true"}, "=1"),  # only exactly "1" opts in
        ({"TEST_KEY": "  "}, "set TEST_KEY"),
        ({"TEST_FREE_ON": "2026-10-13"}, "8 days old"),
        ({"TEST_FREE_ON": "2026-10-22"}, "in the future"),
        ({"TEST_FREE_ON": "yesterday"}, "must be a date"),
    ],
)
def test_the_guard_refuses_before_anything_is_sent(
    tmp_path: Path, environ: dict[str, str], message: str
) -> None:
    keeper = guard(tmp_path, **environ)
    with pytest.raises(ConfigError, match=message):
        keeper.preflight("m-free", 100)
    assert keeper.ledger.window("free", utcnow() - timedelta(days=2)) == (0, 0)


def test_a_week_old_confirmation_is_still_accepted(tmp_path: Path) -> None:
    guard(tmp_path, TEST_FREE_ON="2026-10-14").preflight("m-free", 100)


def test_only_allowlisted_models_are_sent(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not in the free-tier allowlist"):
        guard(tmp_path).preflight("m-paid", 100)


def test_the_per_run_cap(tmp_path: Path) -> None:
    keeper = FreeTierGuard(
        "free",
        FREE.model_copy(update={"caps": CAPS.model_copy(update={"requests_per_minute": 10})}),
        ledger(tmp_path),
        scope="free",
        environ=OWNER,
        today=TODAY,
    )
    for _ in range(3):
        keeper.preflight("m-free", 10)
    with pytest.raises(QuotaExhausted, match="3 requests per run"):
        keeper.preflight("m-free", 10)


def test_the_limit_header_snapshot_survives_trace_redaction() -> None:
    """Short key names: the redactor hides string values under names containing "token"."""
    headers = {
        "x-ratelimit-limit-requests": "1000",
        "x-ratelimit-remaining-tokens": "7000",
        "x-ratelimit-reset-tokens": "7.66s",
        "retry-after": "2",
    }
    snapshot = rate_limit_snapshot(headers)
    assert snapshot == {
        "rpd_limit": "1000",
        "tpm_remaining": "7000",
        "tpm_reset": "7.66s",
        "retry_after": "2",
    }
    assert Redactor(environ={})({"rate_limit": snapshot}) == {"rate_limit": snapshot}
