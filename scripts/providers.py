"""Inspect RECAP's decision providers: readiness, free-tier usage, and stops.

    python scripts/providers.py                   # every provider and whether it could run now
    python scripts/providers.py usage             # live LLM requests and tokens, per provider
    python scripts/providers.py clear-stop groq   # after the owner re-checked the billing page

Nothing here runs inference or calls a hosted API. "check" asks only a LOCAL Ollama server
which models are installed, and a key is reported only as present or missing.

A provider is "stopped" when its free-plan tripwire fired (recap/providers/quota.py): a
response showed a non-free rate limit, HTTP 402, or blocked_api_access. Clear the stop only
after confirming in the provider's console that the account is still on the free plan
with no payment method, and update the dated confirmation (e.g.
RECAP_GROQ_FREE_PLAN_CONFIRMED_ON) at the same time.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from recap.core.config import load_settings
from recap.core.ids import utcnow
from recap.providers.catalog import provider_status
from recap.providers.quota import UsageLedger


async def check() -> int:
    settings = load_settings()
    ledger = UsageLedger(settings.paths.recap_db)
    for entry in await provider_status(settings, ledger):
        default = " (server default)" if entry["default"] else ""
        model = f" · {entry['model']}" if entry["model"] else ""
        print(f"{entry['name']:<14} {entry['origin']:<16} {entry['status']:<12}{model}{default}")
        print(f"{'':<14} {entry['detail']}")
        if "key_env" in entry:
            present = "present" if entry["key_present"] else "missing"
            confirmed = entry["confirmed_on"] or "never"
            print(f"{'':<14} key {entry['key_env']}: {present} · free plan confirmed: {confirmed}")
    return 0


def usage() -> int:
    settings = load_settings()
    ledger = UsageLedger(settings.paths.recap_db)
    now = utcnow()
    for name, spec in settings.providers.providers.items():
        if spec.kind != "llm":
            continue
        minute = ledger.window(name, now - timedelta(minutes=1))
        day = ledger.window(name, now - timedelta(days=1))
        print(
            f"{name:<12} last minute: {minute[0]} requests, {minute[1]} tokens · "
            f"last 24 h: {day[0]} requests, {day[1]} tokens"
        )
        limit = ledger.last_rate_limit(name)
        if limit:
            print(f"{'':<12} last rate-limit headers: {limit}")
        stop = ledger.stopped(name)
        if stop:
            print(f"{'':<12} STOPPED at {stop['stopped_at']}: {stop['reason']}")
    return 0


def clear_stop(name: str) -> int:
    settings = load_settings()
    ledger = UsageLedger(settings.paths.recap_db)
    if ledger.clear_stop(name):
        print(f"cleared the stop on {name}. Remember to update its dated free-plan confirmation.")
        return 0
    print(f"{name} was not stopped")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "command", nargs="?", default="check", choices=["check", "usage", "clear-stop"]
    )
    parser.add_argument("provider", nargs="?", help="provider name (clear-stop only)")
    args = parser.parse_args()
    if args.command == "usage":
        return usage()
    if args.command == "clear-stop":
        if not args.provider:
            parser.error("clear-stop needs a provider name")
        return clear_stop(args.provider)
    return asyncio.run(check())


if __name__ == "__main__":
    raise SystemExit(main())
