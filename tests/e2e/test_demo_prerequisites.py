"""scripts/demo.py prepares a demo's prerequisite where the demo's own run will read it.

The memory-caution demo needs a verified bad-deployment episode in memory. A run reads its
own kind of memory (the scripted policy TRAIN + DEMO, a real model TRAIN + LIVE), so the
prerequisite must come from the demo's own provider: a scripted episode never stands in for
a model's, and a replay, which never writes memory, cannot make one. The fake Ollama server
stands in for the model; demo.py's own main() runs, with its Runtime built over that fake.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import pytest

from recap.core.config import Settings
from recap.core.models import EpisodeOutcome, MemoryPartition, RunConfig
from recap.providers.factory import create_embedder, create_provider
from recap.runtime import Runtime
from scripts import demo
from tests.llm_fakes import FakeOllama, ScriptedModel

pytestmark = pytest.mark.e2e  # demo.py runs whole incidents through the MCP server


def offline(settings: Settings, made: list[Runtime]) -> Callable[..., Runtime]:
    """demo.py's `Runtime(...)`, building each run's provider by name over a fake Ollama."""
    fake = FakeOllama(responder=ScriptedModel())

    def build(**kwargs: Any) -> Runtime:
        runtime = Runtime(settings, **kwargs)

        def provider(config: RunConfig) -> Any:
            name = config.provider or runtime.settings.providers.default
            return create_provider(
                name,
                runtime.settings,
                planner=config.planner,
                mock_http=fake.mock(),
                ledger=runtime.ledger,
            )

        runtime.make_provider = provider  # type: ignore[method-assign]
        runtime.make_embedder = (  # type: ignore[method-assign]
            lambda p: create_embedder(runtime.settings, p, mock_http=fake.mock())
        )
        made.append(runtime)
        return runtime

    return build


async def run_demo(monkeypatch: pytest.MonkeyPatch, settings: Settings, *args: str) -> Runtime:
    made: list[Runtime] = []
    monkeypatch.setattr(demo, "Runtime", offline(settings, made))
    monkeypatch.setattr(sys, "argv", ["demo.py", "--approve", "auto", *args])
    await demo.main()
    return made[-1]


def verified(runtime: Runtime, partition: MemoryPartition, scenario: str) -> list[str]:
    return [
        ep.episode_id
        for ep in runtime.memory.list([partition])
        if ep.scenario_id == scenario and ep.verified and ep.outcome == EpisodeOutcome.SUCCESS
    ]


def memory_hits(runtime: Runtime, scenario: str) -> list[str]:
    [record] = [r for r in runtime.runs.list() if r.scenario_id == scenario]
    return [hit.episode.episode_id for hit in record.memory_hits]


async def test_a_model_demo_reads_the_prerequisite_its_own_model_made(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the prerequisite always ran with the default (scripted) provider and went
    to DEMO, so `--provider ollama` read TRAIN + LIVE and had nothing to be careful about."""
    runtime = await run_demo(
        monkeypatch, settings, "--scenario", "memory-caution", "--provider", "ollama"
    )

    [prerequisite] = verified(runtime, MemoryPartition.LIVE, "bad-deployment")
    episode = runtime.memory.get(prerequisite)
    assert episode is not None and episode.provenance["origin"] == "local_llm"
    assert runtime.memory.list([MemoryPartition.DEMO]) == []  # no scripted stand-in
    assert prerequisite in memory_hits(runtime, "memory-caution")


async def test_with_a_model_as_the_default_the_prerequisite_is_made_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: with RECAP_PROVIDER=ollama the prerequisite went to LIVE, but only DEMO was
    checked, so every demo re-ran the model prerequisite (minutes each) and added an episode."""
    providers = settings.providers.model_copy(update={"default": "ollama"})
    settings = settings.model_copy(update={"providers": providers})

    await run_demo(monkeypatch, settings, "--scenario", "memory-caution")
    first = capsys.readouterr().out
    runtime = await run_demo(monkeypatch, settings, "--scenario", "memory-caution")
    second = capsys.readouterr().out

    assert len(verified(runtime, MemoryPartition.LIVE, "bad-deployment")) == 1
    assert "prerequisite: running 'bad-deployment'" in first
    assert "takes a few minutes" in first  # a plain warning before a slow model run
    assert "prerequisite" not in second


async def test_a_replay_never_gets_a_scripted_stand_in(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A replay never writes memory, so it cannot make the prerequisite; a scripted run would
    put its episode where the replay never reads (DEMO). Nothing runs, and the script says so."""
    runtime = await run_demo(
        monkeypatch, settings, "--scenario", "memory-caution", "--provider", "replay"
    )

    assert runtime.memory.count() == 0
    assert [r.scenario_id for r in runtime.runs.list()] == ["memory-caution"]
    assert "a replay never writes memory" in capsys.readouterr().out


async def test_a_scripted_demo_still_makes_its_own_prerequisite(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await run_demo(monkeypatch, settings, "--scenario", "memory-caution")

    [prerequisite] = verified(runtime, MemoryPartition.DEMO, "bad-deployment")
    assert runtime.memory.list([MemoryPartition.LIVE]) == []
    assert prerequisite in memory_hits(runtime, "memory-caution")
