"""Tests for recordings and replay (recap/providers/transport.py, factory "replay").

A replay runs the SAME provider code against recorded HTTP answers: no network, no model,
and never a guessed answer. It is how CI exercises a real-model run offline.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from recap.core.config import Settings
from recap.core.errors import ConfigError, ReplayMismatch
from recap.providers.factory import REPLAY_ENV, create_provider
from recap.providers.http_llm import HttpLLMProvider
from recap.providers.protocol import FINISH_TOOL
from recap.providers.quota import OPT_IN_ENV
from recap.providers.transport import Recording, fingerprint, normalize
from tests.llm_fakes import (
    FAKE_GROQ_KEY,
    FakeGroq,
    FakeOllama,
    evidence,
    hypothesis,
    investigation_request,
    step,
    tool_answer,
)

OLD = evidence("ev-aaaaaa-001", "mcp:get_recent_deployments(service=checkout)", "v2.4.1 deployed")
NEW = evidence("ev-bbbbbb-001", "mcp:get_recent_deployments(service=checkout)", "v2.4.1 deployed")


async def record_one_decision(settings: Settings, fake: FakeOllama, request) -> tuple[str, object]:
    provider = create_provider("ollama", settings, mock_http=fake.mock())
    decision = await provider.next_action(request)
    assert isinstance(provider, HttpLLMProvider) and provider.info.recording
    return provider.info.recording, decision


def replayer(
    settings: Settings, recording: str, monkeypatch: pytest.MonkeyPatch
) -> HttpLLMProvider:
    monkeypatch.setenv(REPLAY_ENV, str(settings.paths.root / recording))
    provider = create_provider("replay", settings)  # no mock: any network use would fail
    assert isinstance(provider, HttpLLMProvider)
    return provider


async def test_a_replay_gives_the_recorded_decision_offline(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = investigation_request(settings)
    fake = FakeOllama([tool_answer(("get_service_health", {"service": "checkout"}))])
    recording, live = await record_one_decision(settings, fake, request)

    provider = replayer(settings, recording, monkeypatch)
    replayed = await provider.next_action(request)

    assert replayed.tool_calls == live.tool_calls  # type: ignore[attr-defined]
    info = provider.info
    assert (info.kind, info.origin, info.name) == ("replay", "replay", "replay:ollama:qwen3:8b")
    assert info.replay_of["original_provider"] == "ollama"  # type: ignore[index]
    assert info.replay_of["model"] == "qwen3:8b"  # type: ignore[index]
    [call] = replayed.model_calls
    assert call.replayed is True and call.input_tokens == 1200  # the original run's counts
    assert replayed.usage.model_requests == 0  # nothing was sent to a model now
    assert replayed.usage.actual_input_tokens is None


async def test_a_request_that_was_never_recorded_fails_loudly(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOllama([tool_answer(("list_services", {}))])
    recording, _ = await record_one_decision(settings, fake, investigation_request(settings))

    provider = replayer(settings, recording, monkeypatch)
    different = investigation_request(settings, step_number=2)  # a prompt never recorded
    with pytest.raises(ReplayMismatch, match="record the run again"):
        await provider.next_action(different)


async def test_each_recorded_answer_is_used_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = investigation_request(settings)
    fake = FakeOllama([tool_answer(("list_services", {}))])
    recording, _ = await record_one_decision(settings, fake, request)

    provider = replayer(settings, recording, monkeypatch)
    await provider.next_action(request)
    with pytest.raises(ReplayMismatch):
        await provider.next_action(request)


async def test_cited_evidence_ids_are_moved_to_the_current_run(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence IDs carry their run's prefix; a replayed citation must point at THIS run."""
    finish = {
        "kind": "hypothesis",
        "summary": "bad release",
        "hypotheses": [hypothesis("checkout", ["ev-aaaaaa-001"])],
        "next_checks": [],
    }
    fake = FakeOllama([tool_answer((FINISH_TOOL, finish))])
    recording, live = await record_one_decision(
        settings, fake, investigation_request(settings, seen=[OLD])
    )
    assert live.hypotheses[0].supporting_evidence_ids == ["ev-aaaaaa-001"]  # type: ignore[attr-defined]

    provider = replayer(settings, recording, monkeypatch)
    replayed = await provider.next_action(investigation_request(settings, seen=[NEW]))

    assert replayed.hypotheses[0].supporting_evidence_ids == ["ev-bbbbbb-001"]


async def test_a_free_hosted_recording_replays_without_any_live_permission(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OPT_IN_ENV, "1")
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("RECAP_GROQ_FREE_PLAN_CONFIRMED_ON", date.today().isoformat())
    request = investigation_request(settings)
    fake = FakeGroq([step("stop", summary="tampered data")])
    live_provider = create_provider("groq", settings, mock_http=fake.mock())
    await live_provider.next_action(request)
    recording = live_provider.info.recording
    assert recording is not None
    assert FAKE_GROQ_KEY not in (settings.paths.root / recording).read_text()

    for name in (OPT_IN_ENV, "GROQ_API_KEY", "RECAP_GROQ_FREE_PLAN_CONFIRMED_ON"):
        monkeypatch.delenv(name)  # a replay sends nothing, so it needs none of these
    provider = replayer(settings, recording, monkeypatch)
    decision = await provider.next_action(request)
    assert "tampered data" in decision.summary
    assert provider.info.origin == "replay"


def test_fingerprints_ignore_random_ids_only() -> None:
    a = {"messages": [{"content": "Incident ID: inc-0123456789 cites ev-abcdef-007"}]}
    b = {"messages": [{"content": "Incident ID: inc-9876543210 cites ev-123456-007"}]}
    c = {"messages": [{"content": "Incident ID: inc-9876543210 cites ev-123456-008"}]}
    assert fingerprint("POST", "/api/chat", a) == fingerprint("POST", "/api/chat", b)
    assert fingerprint("POST", "/api/chat", a) != fingerprint("POST", "/api/chat", c)
    assert normalize("run-0123456789 ep-0123456789") == "run-* ep-*"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "recording not found"),
        ('{"type": "exchange"}\n', "no header line"),
        ('{"type": "header", "schema": "something-else"}\n', "unsupported recording schema"),
    ],
)
def test_a_broken_recording_is_refused(tmp_path: Path, content: str | None, message: str) -> None:
    path = tmp_path / "recording.jsonl"
    if content is not None:
        path.write_text(content)
    with pytest.raises(ConfigError, match=message):
        Recording.load(path)


def test_the_replay_provider_needs_a_recording(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        REPLAY_ENV, str(settings.paths.root / "fixtures" / "recordings" / "missing.jsonl")
    )
    with pytest.raises(ConfigError, match="recording not found"):
        create_provider("replay", settings)


def test_a_recording_header_holds_no_secret_values(settings: Settings) -> None:
    """The header stores the provider's settings: env var NAMES only, never their values."""
    header_settings = settings.providers.providers["groq"].model_dump(mode="json")
    text = json.dumps(header_settings)
    assert "GROQ_API_KEY" in text and FAKE_GROQ_KEY not in text
