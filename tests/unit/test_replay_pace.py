"""A paced replay (RunConfig.replay_pace) waits a share of each recorded answer's duration.

Pacing exists for demos: a real two-specialist run took about 11 minutes, its replay answers
in seconds, too fast to watch. A pace changes only WHEN an answer comes, never which answer
(requests are matched by what they say, stops by their place).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from recap.providers import transport as transport_module
from recap.providers.transport import Recording, ReplayTransport, fingerprint

BODY: dict[str, Any] = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def recording(duration_ms: float) -> Recording:
    response = {"status": 200, "body": {"done": True}, "duration_ms": duration_ms}
    exchange = {
        "type": "exchange",
        "seq": 1,
        "method": "POST",
        "path": "/api/chat",
        "fingerprint": fingerprint("POST", "/api/chat", BODY),
        "request": BODY,
        "response": response,
    }
    return Recording(path=str(Path("r.jsonl")), header={"type": "header"}, exchanges=[exchange])


@pytest.mark.parametrize(
    ("duration_ms", "pace", "waited"),
    [
        (40_000.0, 0.0, None),  # no pace: no wait at all
        (40_000.0, 0.05, 2.0),  # 5% of a 40 s answer
        (600_000.0, 1.0, ReplayTransport.MAX_PACED_WAIT_SECONDS),  # never a stalled demo
    ],
)
async def test_a_paced_replay_waits_a_share_of_the_recorded_time(
    monkeypatch: pytest.MonkeyPatch, duration_ms: float, pace: float, waited: float | None
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(transport_module.asyncio, "sleep", fake_sleep)
    replay = ReplayTransport(recording(duration_ms), pace=pace)
    exchange = await replay.request("POST", "/api/chat", BODY, timeout=30)

    assert exchange.replayed and exchange.body == {"done": True}  # the same answer
    assert sleeps == ([] if waited is None else [pytest.approx(waited)])


def test_the_pace_is_kept_within_zero_and_one() -> None:
    assert ReplayTransport(recording(1.0), pace=-1).pace == 0.0
    assert ReplayTransport(recording(1.0), pace=7).pace == 1.0
