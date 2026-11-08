"""A conservative, time-based AWS cost meter for one bounded VM window (M6 W5).

AWS billing data arrives hours late, so it cannot stop spending in time. This meter estimates
the window's spend from the VM's own state, read with free describe calls, and prices pinned
from the AWS Price List API (queried 2026-11-09, ap-south-1):

    compute   every running hour at $0.2222 (m6a.2xlarge, on demand, Linux), plus $0.005
              for its public IPv4 address
    storage   every hour of the window at $0.01874 (150 GB gp3 at $0.0912 per GB-month of
              730 hours), whether the VM runs or not

A session starts at the instance's LaunchTime (its last start) and ends at the stop time AWS
records with the state. Whatever is not known is counted up to now, so the estimate can be
too high, never too low. The stop line is the cap less a safety margin (default 30%): at or
past it, or when one more step would cross it, `status` exits 3 and no new work may start.

    python scripts/aws_cost_meter.py open --cap 5      # at the owner's approval, once
    python scripts/aws_cost_meter.py status            # before every step; exit 3 = stop
    python scripts/aws_cost_meter.py close             # after the final stop

The ledger lives outside git (~/.config/recap/aws/w5-cost.json, mode 600). Nothing here
starts, stops or changes anything on AWS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".config" / "recap" / "aws"
LEDGER = STATE_DIR / "w5-cost.json"
INSTANCE_FILE = STATE_DIR / "recap-m5.env"  # infra/aws/ec2.sh keeps the instance id there
EXIT_STOP = 3


@dataclass(frozen=True)
class Prices:
    """USD per hour, pinned from the AWS Price List API (2026-11-09, ap-south-1)."""

    instance: float = 0.2222  # m6a.2xlarge, on demand, Linux, shared tenancy
    public_ipv4: float = 0.005  # while the instance runs with its public address
    storage: float = 150 * 0.0912 / 730  # 150 GB gp3, $0.0912 per GB-month

    @property
    def running(self) -> float:
        return self.instance + self.public_ipv4


PRICES = Prices()


@dataclass(frozen=True)
class Observed:
    """What AWS says about the instance now."""

    state: str  # pending, running, stopping, stopped, ...
    launch_time: datetime | None  # its last start
    stop_time: datetime | None  # when it was last stopped, if AWS says so


@dataclass(frozen=True)
class Estimate:
    running_hours: float
    compute_usd: float
    storage_usd: float
    total_usd: float
    stop_line_usd: float

    @property
    def remaining_usd(self) -> float:
        return self.stop_line_usd - self.total_usd


def parse_stop_time(reason: str) -> datetime | None:
    """The time in AWS's StateTransitionReason, e.g. "User initiated (2026-11-07 09:30:22 GMT)"."""
    found = re.search(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) GMT\)", reason or "")
    if not found:
        return None
    return datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def update(ledger: dict[str, Any], observed: Observed, now: datetime) -> dict[str, Any]:
    """The ledger with the instance's sessions brought up to date (a new dict)."""
    start = _time(ledger["window_start"])
    sessions = [dict(s) for s in ledger.get("sessions", [])]
    open_ = next((s for s in sessions if s.get("end") is None), None)
    launch = observed.launch_time
    if observed.state in ("pending", "running", "stopping") and launch is not None:
        began = max(launch, start)  # time before the window is not this window's
        if open_ is not None and _time(open_["start"]) != began:
            # A later start than the open session's: it stopped in between unseen. Counted as
            # running until the new start (too high, never too low).
            open_["end"] = began.isoformat()
            open_ = None
        if open_ is None and not any(_time(s["start"]) == began for s in sessions):
            sessions.append({"start": began.isoformat(), "end": None})
    elif observed.state == "stopped" and open_ is not None:
        stopped = observed.stop_time
        began = _time(open_["start"])
        open_["end"] = (stopped if stopped is not None and stopped >= began else now).isoformat()
    return {**ledger, "sessions": sessions}


def estimate(ledger: dict[str, Any], now: datetime, prices: Prices = PRICES) -> Estimate:
    start = _time(ledger["window_start"])
    end = _time(ledger["window_end"]) if ledger.get("window_end") else now
    hours = 0.0
    for session in ledger.get("sessions", []):
        began = max(_time(session["start"]), start)
        stopped = _time(session["end"]) if session.get("end") else now
        hours += max(0.0, (stopped - began).total_seconds() / 3600)
    compute = hours * prices.running
    storage = max(0.0, (end - start).total_seconds() / 3600) * prices.storage
    cap = float(ledger["cap_usd"])
    margin = float(ledger.get("margin", 0.3))
    return Estimate(
        running_hours=round(hours, 3),
        compute_usd=round(compute, 4),
        storage_usd=round(storage, 4),
        total_usd=round(compute + storage, 4),
        stop_line_usd=round(cap * (1 - margin), 4),
    )


def may_continue(found: Estimate, next_hours: float, prices: Prices = PRICES) -> bool:
    """Whether one more step of `next_hours` (running) stays under the stop line."""
    return found.total_usd + next_hours * (prices.running + prices.storage) < found.stop_line_usd


def _time(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --- AWS (read only) and the ledger file -------------------------------------------------------


def observe_instance() -> Observed:
    """One free describe call; nothing is changed."""
    instance = _instance_id()
    out = subprocess.run(
        [
            "aws",
            "ec2",
            "describe-instances",
            "--instance-ids",
            instance,
            "--query",
            "Reservations[0].Instances[0].[State.Name,LaunchTime,StateTransitionReason]",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env={
            **os.environ,
            "AWS_PROFILE": os.environ.get("AWS_PROFILE", "amlc-personal"),
            "AWS_REGION": os.environ.get("AWS_REGION", "ap-south-1"),
            "AWS_PAGER": "",
        },
    )
    state, launch, reason = json.loads(out.stdout)
    return Observed(
        state=str(state),
        launch_time=_time(launch) if launch else None,
        stop_time=parse_stop_time(str(reason or "")),
    )


def _instance_id() -> str:
    text = INSTANCE_FILE.read_text(encoding="utf-8") if INSTANCE_FILE.is_file() else ""
    found = re.search(r"^INSTANCE_ID=(i-[0-9a-f]+)$", text, flags=re.MULTILINE)
    if not found:
        raise SystemExit(f"no instance id in {INSTANCE_FILE} (infra/aws/ec2.sh keeps it there)")
    return found.group(1)


def load_ledger() -> dict[str, Any]:
    if not LEDGER.is_file():
        raise SystemExit("no cost window is open: run `open --cap <USD>` at the owner's approval")
    data: dict[str, Any] = json.loads(LEDGER.read_text(encoding="utf-8"))
    return data


def save_ledger(ledger: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=1) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(LEDGER)


def report(found: Estimate, ledger: dict[str, Any], observed: Observed) -> str:
    return (
        f"window since {ledger['window_start']} (cap ${float(ledger['cap_usd']):.2f}, stop line "
        f"${found.stop_line_usd:.2f}): instance {observed.state}; {found.running_hours:.2f} "
        f"running hours = ${found.compute_usd:.2f} compute, ${found.storage_usd:.2f} disk; "
        "estimated "
        f"${found.total_usd:.2f} so far, ${found.remaining_usd:.2f} before the stop line"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    opened = sub.add_parser("open", help="start the window (at the owner's approval)")
    opened.add_argument("--cap", type=float, required=True, help="the approved cap in USD")
    opened.add_argument("--margin", type=float, default=0.3, help="safety margin (default 0.3)")
    status = sub.add_parser("status", help="the estimate so far; exit 3 at the stop line")
    status.add_argument(
        "--next-hours",
        type=float,
        default=1.0,
        help="the next step's running time, which must also fit (default 1 h: one problem)",
    )
    sub.add_parser("close", help="end the window (after the VM's final stop)")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    if args.command == "open":
        if LEDGER.is_file() and not load_ledger().get("window_end"):
            print("a cost window is already open: close it first", file=sys.stderr)
            return 2
        save_ledger(
            {
                "window_start": now.isoformat(),
                "window_end": None,
                "cap_usd": args.cap,
                "margin": args.margin,
                "sessions": [],
            }
        )
        print(f"cost window opened at {now.isoformat()} with a cap of ${args.cap:.2f}")
        return 0
    ledger = load_ledger()
    observed = observe_instance()
    ledger = update(ledger, observed, now)
    if args.command == "close":
        if observed.state not in ("stopped", "terminated"):
            print(f"the instance is {observed.state}: stop it before closing", file=sys.stderr)
            return 2
        ledger["window_end"] = now.isoformat()
    save_ledger(ledger)
    found = estimate(ledger, now)
    print(report(found, ledger, observed))
    if args.command == "status" and not may_continue(found, args.next_hours):
        print(
            f"STOP: another {args.next_hours:g} h would cross the stop line; start no new work",
            file=sys.stderr,
        )
        return EXIT_STOP
    return 0


if __name__ == "__main__":
    sys.exit(main())
