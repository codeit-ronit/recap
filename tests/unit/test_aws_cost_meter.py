"""The W5 cost meter (scripts/aws_cost_meter.py): a time-based estimate from the VM's own
state, conservative wherever AWS does not say when something happened, with a stop line
below the approved cap. No AWS call is made here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.aws_cost_meter import Observed, Prices, estimate, may_continue, parse_stop_time, update

T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
PRICES = Prices()


def ledger(**extra: object) -> dict[str, object]:
    return {
        "window_start": T0.isoformat(),
        "window_end": None,
        "cap_usd": 5.0,
        "margin": 0.3,
        "sessions": [],
        **extra,
    }


def test_the_pinned_prices_are_the_price_lists() -> None:
    assert PRICES.instance == 0.2222 and PRICES.public_ipv4 == 0.005
    assert round(PRICES.storage * 24, 3) == 0.45  # 150 GB gp3: about $0.45 a day


def test_a_running_session_counts_from_its_start_to_now_and_the_disk_all_window() -> None:
    now = T0 + timedelta(hours=2)
    running = Observed("running", launch_time=T0 + timedelta(minutes=30), stop_time=None)
    found = estimate(update(ledger(), running, now), now)
    assert found.running_hours == 1.5
    assert found.compute_usd == round(1.5 * PRICES.running, 4)
    assert found.storage_usd == round(2 * PRICES.storage, 4)
    assert found.stop_line_usd == 3.5  # the $5 cap less 30%


def test_a_stop_ends_the_session_at_the_time_aws_records() -> None:
    now = T0 + timedelta(hours=5)
    started = update(ledger(), Observed("running", T0, None), T0 + timedelta(hours=1))
    stopped = update(started, Observed("stopped", T0, T0 + timedelta(hours=3)), now)
    assert stopped["sessions"][0]["end"] == (T0 + timedelta(hours=3)).isoformat()
    assert estimate(stopped, now).running_hours == 3.0


def test_what_aws_does_not_say_is_counted_up_to_now_never_less() -> None:
    now = T0 + timedelta(hours=4)
    started = update(ledger(), Observed("running", T0, None), T0 + timedelta(hours=1))
    # Stopped, but no readable stop time: counted as running until now.
    unknown = update(started, Observed("stopped", T0, None), now)
    assert estimate(unknown, now).running_hours == 4.0
    # Stopped and started again unseen: the first session runs until the second one started.
    again = update(started, Observed("running", T0 + timedelta(hours=3), None), now)
    assert [s["end"] for s in again["sessions"]] == [(T0 + timedelta(hours=3)).isoformat(), None]
    assert estimate(again, now).running_hours == 4.0


def test_time_before_the_window_is_not_counted() -> None:
    now = T0 + timedelta(hours=1)
    early = Observed("running", launch_time=T0 - timedelta(hours=10), stop_time=None)
    assert estimate(update(ledger(), early, now), now).running_hours == 1.0


def test_no_new_step_may_start_when_it_would_cross_the_stop_line() -> None:
    now = T0 + timedelta(hours=12)
    long_running = update(ledger(), Observed("running", T0, None), now)
    found = estimate(long_running, now)  # 12 h: about $2.73 compute and $0.22 disk
    assert found.total_usd < found.stop_line_usd
    assert may_continue(found, next_hours=1.0)
    assert not may_continue(found, next_hours=3.0)


def test_aws_stop_reasons_are_read_for_their_time() -> None:
    assert parse_stop_time("User initiated (2026-11-07 09:30:22 GMT)") == datetime(
        2026, 11, 7, 9, 30, 22, tzinfo=UTC
    )
    assert parse_stop_time("") is None and parse_stop_time("Server.ScheduledStop") is None
