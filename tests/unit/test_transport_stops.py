"""What a recording keeps so that a replay does not depend on timing (review of M3).

recap/providers/transport.py: a request that failed before any answer (the server could
not be reached, or did not answer in time) is recorded, and the replay fails the same way;
the run's stops are written between the exchanges and raised by the replay in place. A
replay that asks for something the recording lacks says what may have changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from recap.core.errors import BudgetExhausted, ProviderError, ReplayMismatch
from recap.core.models import RunBudget
from recap.harness.budget import BudgetTracker, acting
from recap.providers.transport import (
    LiveTransport,
    Recorder,
    Recording,
    RecordingTransport,
    ReplayTransport,
    bind_stops,
)

BASE_URL = "http://127.0.0.1:11434"


def recording_transport(path: Path, handler: Any) -> RecordingTransport:
    live = LiveTransport(BASE_URL, mock=httpx.MockTransport(handler))
    return RecordingTransport(live, Recorder(path, {"recording_id": "test"}))


async def test_a_request_that_failed_is_recorded_and_fails_the_same_way_in_the_replay(
    tmp_path: Path,
) -> None:
    """The first request cannot reach the server, the second is answered. The replay must
    fail the first the same way (not with ReplayMismatch) and answer the second, so that
    whatever the run did after the failure (a retry, a tool error) lines up."""
    sent: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"version": "0.0.0-fake"})

    path = tmp_path / "recording.jsonl"
    live = recording_transport(path, server)
    with pytest.raises(ProviderError) as failed:
        await live.request("GET", "/api/version", timeout=5)
    answered = await live.request("GET", "/api/version", timeout=5)

    replay = ReplayTransport(Recording.load(path))
    with pytest.raises(ProviderError) as again:
        await replay.request("GET", "/api/version", timeout=5)
    assert not isinstance(again.value, ReplayMismatch)
    assert str(again.value) == str(failed.value)
    second = await replay.request("GET", "/api/version", timeout=5)
    assert second.body == answered.body and second.replayed


async def test_a_replay_mismatch_names_every_likely_cause(tmp_path: Path) -> None:
    """Memory (an episode the original run never saw) and a timing stop recorded before
    RECAP recorded stops change requests just as a prompt change does."""
    path = tmp_path / "recording.jsonl"
    live = recording_transport(path, lambda request: httpx.Response(200, json={"version": "1"}))
    await live.request("GET", "/api/version", timeout=5)

    replay = ReplayTransport(Recording.load(path))
    with pytest.raises(ReplayMismatch) as mismatch:
        await replay.request("POST", "/api/chat", {"messages": []}, timeout=5)
    message = str(mismatch.value)
    for cause in ("prompts", "tools", "tool results", "memory", "timing"):
        assert cause in message, cause
    assert "record the run again" in message


async def test_a_recording_keeps_the_run_s_stops_and_the_replay_raises_them_in_place(
    tmp_path: Path,
) -> None:
    """Every request through the run's recording, the embedder's included (it shares the
    recording), is a check of the acting agent; a stop is written between the exchanges
    and raised by the replay at the same place."""
    path = tmp_path / "recording.jsonl"
    chat = recording_transport(path, lambda request: httpx.Response(200, json={"version": "1"}))
    embedder = RecordingTransport(
        LiveTransport(BASE_URL, mock=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
        chat.recorder,
    )
    live = BudgetTracker(RunBudget(max_tool_calls=0))
    bind_stops(chat, live)
    with acting("telemetry_investigator"):
        await chat.request("GET", "/api/version", timeout=5)  # check 1
        await embedder.request("POST", "/api/embed", {"input": ["q"]}, timeout=5)  # check 2
        with pytest.raises(BudgetExhausted):
            live.reserve_tool_call()  # check 3: refused

    loaded = Recording.load(path)
    assert len(loaded.exchanges) == 2
    assert [(s["actor"], s["check"], s["at"]) for s in loaded.stops] == [
        ("telemetry_investigator", 3, "tool_call")
    ]

    replay = ReplayTransport(loaded)
    again = BudgetTracker(RunBudget())  # room for calls: only the recording refuses
    bind_stops(replay, again)
    assert again.replaying
    with acting("telemetry_investigator"):
        await replay.request("GET", "/api/version", timeout=5)
        await replay.request("POST", "/api/embed", {"input": ["q"]}, timeout=5)
        with pytest.raises(BudgetExhausted, match="0/0 tool calls used"):
            again.reserve_tool_call()
