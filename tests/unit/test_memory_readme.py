"""recap/memory/README.md: its usage example is what callers copy, so it runs here as written.

Before M3's review it chose the access with `MemoryAccess.from_run_config` (DEMO unless a
run sets partitions: a real-model run read the scripted policy's memory and wrote its own
experience there) and checked reuse without the restore points, so a remembered rollback to
a version today's evidence does not name was still "safe to reuse".
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from recap.agents.verifier import EvidenceVerifier
from recap.core.config import Settings
from recap.core.models import (
    ActionRecord,
    AgentFinding,
    AgentRole,
    EnvironmentInfo,
    EpisodeOutcome,
    ExperienceEpisode,
    FailureMechanism,
    IncidentState,
    MemoryPartition,
    ProviderInfo,
    RecoveryResult,
    RunConfig,
    RunRecord,
    ScenarioInfo,
)
from recap.memory.store import ExperienceStore, MemoryAccess
from tests.unit.test_verifier import ALERT, DEPLOY, Run, hypothesis, release_errors

README = Path(__file__).resolve().parents[2] / "recap" / "memory" / "README.md"


def usage_example(readme: str) -> str:
    """The first Python block of the README's "Using it" section."""
    section = readme.split("## Using it", 1)[1]
    found = re.search(r"```python\n(.*?)```", section, re.DOTALL)
    assert found, "the usage section has no Python example"
    return found.group(1)


def remembered(episode_id: str, partition: MemoryPartition) -> ExperienceEpisode:
    """A verified rollback of checkout to v2.4.0, alerted exactly like today's incident."""
    fix = ActionRecord(
        action="execute_rollback",
        target="checkout",
        params={"to_version": "v2.4.0"},
        result="recovered",
        at=ALERT,
    )
    return ExperienceEpisode(
        episode_id=episode_id,
        origin_run_id=f"run-{episode_id}",
        scenario_id="bad-deployment",
        failure_family="bad_deployment",
        partition=partition,
        service="checkout",
        symptoms=["checkout: HTTP 5xx ratio above 30%"],
        failure_mechanism=FailureMechanism.BAD_DEPLOYMENT,
        mechanism_target="checkout",
        successful_action=fix,
        outcome=EpisodeOutcome.SUCCESS,
        verified=True,
        verification_source="environment-health-oracle",
        created_at=ALERT,
    )


def a_model_run(settings: Settings) -> dict[str, Any]:
    """Everything the example names, for a run whose decisions come from a local model.

    Today checkout's v2.4.1 is bad and its evidence names v2.3.9 as the version to restore.
    Memory holds the scripted policy's episode (DEMO) and the model's own (LIVE).
    """
    store = ExperienceStore(settings.paths.memory_db)
    store.add(remembered("ep-scripted", MemoryPartition.DEMO), MemoryAccess.for_origin("scripted"))
    store.add(remembered("ep-model", MemoryPartition.LIVE), MemoryAccess.for_origin("local_llm"))

    run = Run()
    deploy = run.deployments(("checkout", "v2.3.9", -2880), ("checkout", "v2.4.1", -12))
    rate = run.error_rate("checkout", onset=-10)
    logs = run.logs("checkout", release_errors("v2.4.1", range(-11, 1)))
    claim = hypothesis(DEPLOY, "checkout", deploy, rate, logs, version="v2.4.1")
    finding = AgentFinding(
        agent_role=AgentRole.INVESTIGATOR, hypotheses=[claim], started_at=ALERT, finished_at=ALERT
    )
    report = EvidenceVerifier().verify([finding], run.registry, run.incident)
    assert report.best is not None  # the example starts from a verified diagnosis
    fix = ActionRecord(
        action="execute_rollback",
        target="checkout",
        params={"to_version": "v2.3.9"},
        result="recovered",
        at=ALERT,
    )
    record = RunRecord(
        run_id="run-readme",
        scenario_id="bad-deployment",
        config=RunConfig(provider="ollama"),
        incident=run.incident,
        state=IncidentState.RESOLVED,
        environment=EnvironmentInfo(
            kind="simulated", name="simulator", namespace="shop", is_simulated=True
        ),
        verification=report,
        actions=[fix],
        recovery=RecoveryResult(
            healthy_before=False,
            healthy_after=True,
            oracle_name="environment-health-oracle",
            verified=True,
            checked_at=ALERT,
        ),
    )
    info = ProviderInfo(name="ollama:qwen3:8b", kind="llm", origin="local_llm", model="qwen3:8b")
    return {
        "settings": settings,
        "run_config": record.config,  # what a caller has before the controller fills it in
        "provider": SimpleNamespace(info=info),
        "incident": run.incident,
        "report": report,
        "verifier": EvidenceVerifier(),
        "registry": run.registry,
        "record": record,
        "scenario": ScenarioInfo(
            id="bad-deployment",
            title="Checkout errors right after a new release",
            family="bad_deployment",
            partition=MemoryPartition.DEMO,
            alert_service="checkout",
        ),
    }


def test_the_usage_example_uses_the_runs_own_memory_and_checks_the_restore_point(
    settings: Settings,
) -> None:
    names = a_model_run(settings)
    exec(usage_example(README.read_text(encoding="utf-8")), names)  # our own README, as written

    access = names["access"]
    assert access.readable_partitions == [MemoryPartition.TRAIN, MemoryPartition.LIVE]
    assert access.write_partition == MemoryPartition.LIVE
    [hit] = names["hits"]  # the model's own episode, never the scripted policy's
    assert hit.episode.episode_id == "ep-model"
    assert not hit.safe_to_reuse
    assert hit.contradicting == [
        "the remembered rollback restores v2.4.0, but today's evidence names v2.3.9"
    ]
    assert names["stored"] is True and names["episode"].partition == MemoryPartition.LIVE
