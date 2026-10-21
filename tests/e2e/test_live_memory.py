"""Real-model runs keep their own memory: LIVE episodes, reuse checked against live evidence.

The fake Ollama server stands in for a model (fixed answers, no reasoning), so these runs
have the origin of a local model: they write to the LIVE partition, which scripted runs
never read, and a replay of one writes nothing at all.
"""

from __future__ import annotations

import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState, MemoryPartition, RunConfig
from recap.providers.factory import REPLAY_ENV
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from tests.e2e.test_runbook_search import live_run
from tests.llm_fakes import FakeOllama, OperatorApproves, ScriptedModel

pytestmark = pytest.mark.e2e

MODEL = RunConfig(provider="ollama")


async def test_model_runs_write_live_episodes_and_reuse_them_after_a_live_check(
    settings: Settings,
) -> None:
    _, first = await live_run(settings, MODEL, FakeOllama(responder=ScriptedModel()))
    assert first.state == IncidentState.RESOLVED
    assert first.config.memory_write_partition == MemoryPartition.LIVE
    assert first.config.memory_read_partitions == [MemoryPartition.TRAIN, MemoryPartition.LIVE]

    store = Runtime(settings).memory
    [episode] = store.list([MemoryPartition.LIVE])
    assert store.list([MemoryPartition.DEMO]) == []
    assert episode.origin_run_id == first.run_id
    assert episode.provenance["origin"] == "local_llm" and episode.provenance["model"]
    assert episode.fix_preconditions["to_version"] == "v2.4.0"
    assert episode.trajectory and episode.summary_method == "deterministic-extraction"

    # A scripted run never reads the model's episodes.
    scripted_runtime = Runtime(settings)
    scripted = await scripted_runtime.run_incident(
        "bad-deployment", RunConfig(), approval_channel=OperatorApproves(scripted_runtime.approvals)
    )
    assert scripted.memory_hits == []

    # A second model run finds it, and reuses it only after the live evidence confirms it.
    runtime, second = await live_run(settings, MODEL, FakeOllama(responder=ScriptedModel()))
    assert second.state == IncidentState.RESOLVED
    [hit] = second.memory_hits
    assert hit.episode.episode_id == episode.episode_id and hit.similarity == 1.0
    assert hit.safe_to_reuse and not hit.contradicting
    assert "the past fix was independently verified" in hit.supporting
    assessed = [
        e.payload
        for e in runtime.events.list_events(second.run_id)
        if e.category == C.MEMORY_ASSESS
    ]
    assert assessed and assessed[-1]["restore_points"] == ["v2.4.0"]


async def test_a_replay_never_becomes_experience(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, live = await live_run(settings, MODEL, FakeOllama(responder=ScriptedModel()))
    assert live.provider_info is not None and live.provider_info.recording
    before = Runtime(settings).memory.count()

    monkeypatch.setenv(REPLAY_ENV, str(settings.paths.root / live.provider_info.recording))
    runtime = Runtime(settings)
    replay = await runtime.run_incident(
        "bad-deployment",
        RunConfig(provider="replay", use_memory=False),
        approval_channel=OperatorApproves(runtime.approvals),
    )
    assert replay.state == IncidentState.RESOLVED
    assert replay.config.memory_write is False and replay.episode_id is None
    assert Runtime(settings).memory.count() == before
    skips = [
        e.payload for e in runtime.events.list_events(replay.run_id) if e.category == C.MEMORY_SKIP
    ]
    assert "a replay reproduces a recorded run; it is not new experience" in [
        s["reason"] for s in skips
    ]
