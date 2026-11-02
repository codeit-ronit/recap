"""Environment-scoped memory (M5): each kind of target keeps its own experience.

An episode's provenance names the kind of target its run acted on ("simulated", "kind",
...). A search reads only episodes from the incident's own kind, so a run on the real Kind
cluster never finds the simulator's episodes and the simulator never finds a cluster's,
however alike they read. Episodes from before M5 name no environment: every one of them was
simulated, and they count as such, so the simulator's own hits are exactly what they were.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import EnvironmentConfig, Settings
from recap.core.models import (
    ActionRecord,
    EnvironmentInfo,
    EpisodeOutcome,
    ExperienceEpisode,
    FailureMechanism,
    Hypothesis,
    HypothesisStatus,
    Incident,
    IncidentState,
    MemoryHit,
    MemoryPartition,
    RunBudget,
    RunConfig,
    VerificationReport,
)
from recap.memory.consolidation import Consolidator
from recap.memory.similarity import SIMULATED, MemoryQuery
from recap.memory.store import (
    ExperienceStore,
    MemoryAccess,
    assess_reuse,
    encode_vector,
    episode_environment,
)
from recap.runtime import Runtime
from tests.fakes import FakeEnvironment
from tests.llm_fakes import OperatorApproves
from tests.unit.test_consolidation import EVIDENCE, SCENARIO, make_record

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SYMPTOMS = ["checkout: HTTP 5xx ratio above 30% (SLO 1%)", "checkout: p95 latency above 1.5s"]
SIMULATED_RUN = {"simulated": True, "environment": "simulated"}
KIND_RUN = {"simulated": False, "environment": "kind"}


def episode(episode_id: str, provenance: dict[str, Any], **changes: Any) -> ExperienceEpisode:
    """A verified rollback of checkout to v2.4.0, alerted exactly like `query()`."""
    fix = ActionRecord(
        action="execute_rollback",
        target="checkout",
        params={"to_version": "v2.4.0"},
        result="recovered",
        at=T0,
    )
    fields: dict[str, Any] = {
        "episode_id": episode_id,
        "origin_run_id": f"run-{episode_id}",
        "scenario_id": "bad-deployment",
        "failure_family": "bad_deployment",
        "partition": MemoryPartition.DEMO,
        "service": "checkout",
        "symptoms": list(SYMPTOMS),
        "failure_mechanism": FailureMechanism.BAD_DEPLOYMENT,
        "mechanism_target": "checkout",
        "successful_action": fix,
        "outcome": EpisodeOutcome.SUCCESS,
        "verified": True,
        "verification_source": "environment-health-oracle",
        "provenance": dict(provenance),
        "created_at": T0,
        **changes,
    }
    return ExperienceEpisode(**fields)


def query(environment: str | None = None) -> MemoryQuery:
    """Today's incident on checkout; without `environment` it is a simulator incident."""
    if environment is None:
        return MemoryQuery(service="checkout", affected_services=["checkout"], symptoms=SYMPTOMS)
    return MemoryQuery(
        service="checkout",
        affected_services=["checkout"],
        symptoms=SYMPTOMS,
        environment=environment,
    )


def found(store: ExperienceStore, environment: str | None) -> list[str]:
    """IDs of the episodes a scripted run on `environment` finds (DEMO and TRAIN)."""
    hits = store.search(query(environment), MemoryAccess.default(), k=10)
    return [hit.episode.episode_id for hit in hits]


def bad_release_on(target: str) -> VerificationReport:
    best = Hypothesis(
        claim="live diagnosis",
        mechanism=FailureMechanism.BAD_DEPLOYMENT,
        target_service=target,
        status=HypothesisStatus.SUPPORTED,
        details={"version": "v2.4.1"},
    )
    return VerificationReport(status="supported", best=best)


@pytest.fixture
def store(tmp_path: Path) -> ExperienceStore:
    return ExperienceStore(tmp_path / "memory.db")


# --- which kind of target an episode belongs to ----------------------------------------------


@pytest.mark.parametrize(
    ("provenance", "kind"),
    [
        ({}, SIMULATED),  # an episode before M5 (or a planted one) names nothing
        ({"simulated": True}, SIMULATED),
        ({"seed": 11, "provider": "deterministic-scripted-policy"}, SIMULATED),
        ({"environment": ""}, SIMULATED),
        ({"environment": 7}, SIMULATED),  # not a kind: read as if it were missing
        (SIMULATED_RUN, SIMULATED),
        (KIND_RUN, "kind"),
        ({"simulated": False, "environment": "sregym"}, "sregym"),
        # It says it was not simulated, but not where it ran: it matches no run at all.
        ({"simulated": False}, ""),
    ],
)
def test_an_episode_without_an_environment_counts_as_simulated(
    provenance: dict[str, Any], kind: str
) -> None:
    assert episode_environment(episode("ep-1", provenance)) == kind


def test_a_query_from_an_incident_carries_the_incidents_environment() -> None:
    incident = Incident(
        incident_id="inc-1",
        scenario_id="kind-bad-release",
        service="checkout",
        symptoms=SYMPTOMS,
        created_at=T0,
        environment="kind",
        budget=RunBudget(),
    )
    assert MemoryQuery.from_incident(incident).environment == "kind"
    simulator = incident.model_copy(update={"environment": "simulated"})
    assert MemoryQuery.from_incident(simulator).environment == SIMULATED
    assert query().environment == SIMULATED  # a query that names none is a simulator incident


# --- search: hits only from the same kind of target ----------------------------------------


def test_a_kind_run_never_finds_a_simulated_episode(store: ExperienceStore) -> None:
    for planted in [
        episode("ep-sim", SIMULATED_RUN),
        episode("ep-legacy", {"simulated": True}, created_at=T0 + timedelta(minutes=1)),
        episode("ep-bare", {}, created_at=T0 + timedelta(minutes=2)),
    ]:
        store.add(planted, MemoryAccess.default())

    assert found(store, "kind") == []
    # The same episodes, word for word today's alert, are all hits on the simulator.
    assert found(store, None) == ["ep-bare", "ep-legacy", "ep-sim"]


def test_the_simulator_never_finds_a_kind_episode(store: ExperienceStore) -> None:
    store.add(episode("ep-kind", KIND_RUN), MemoryAccess.default())

    assert found(store, None) == []
    assert found(store, SIMULATED) == []
    [hit] = store.search(query("kind"), MemoryAccess.default())
    assert (hit.episode.episode_id, hit.similarity) == ("ep-kind", 1.0)


def test_each_run_finds_only_its_own_kind_in_a_mixed_store(store: ExperienceStore) -> None:
    store.add(episode("ep-sim", SIMULATED_RUN), MemoryAccess.default())
    store.add(
        episode("ep-kind", KIND_RUN, created_at=T0 + timedelta(minutes=1)), MemoryAccess.default()
    )
    sregym = {"simulated": False, "environment": "sregym"}
    store.add(
        episode("ep-sregym", sregym, created_at=T0 + timedelta(minutes=2)), MemoryAccess.default()
    )
    nowhere = {"simulated": False}  # not simulated, no kind: nobody's experience
    store.add(
        episode("ep-nowhere", nowhere, created_at=T0 + timedelta(minutes=3)), MemoryAccess.default()
    )

    assert found(store, None) == ["ep-sim"]
    assert found(store, "kind") == ["ep-kind"]
    assert found(store, "sregym") == ["ep-sregym"]
    assert store.count() == 4  # nothing is hidden from the dashboard's listing
    assert [e.episode_id for e in store.list()] == ["ep-nowhere", "ep-sregym", "ep-kind", "ep-sim"]


def test_partitions_still_apply_within_an_environment(store: ExperienceStore) -> None:
    live = episode("ep-kind-model", KIND_RUN, partition=MemoryPartition.LIVE)
    assert store.add(live, MemoryAccess.for_origin("local_llm"))

    # A scripted run on Kind reads TRAIN + DEMO: a model's Kind episode is not its own.
    assert store.search(query("kind"), MemoryAccess.for_origin("scripted")) == []
    [hit] = store.search(query("kind"), MemoryAccess.for_origin("local_llm"))
    assert hit.episode.episode_id == "ep-kind-model"
    assert store.search(query(), MemoryAccess.for_origin("local_llm")) == []


def test_semantic_recall_stays_within_the_environment(store: ExperienceStore) -> None:
    """A paraphrase (few shared words) is recalled by meaning only from the same kind."""
    close = {"embedding": encode_vector([0.99, 0.1, 0.0]), "embedding_model": "fake-model"}
    words = ["shoppers cannot finish buying anything"]
    store.add(
        episode("ep-sim-para", SIMULATED_RUN, symptoms=words, **close), MemoryAccess.default()
    )
    kind_para = episode("ep-kind-para", KIND_RUN, symptoms=words, **close)
    store.add(kind_para, MemoryAccess.default())

    def recalled(environment: str) -> list[str]:
        hits = store.search(
            query(environment),
            MemoryAccess.default(),
            min_similarity=0.5,  # the paraphrases share no symptom word: only recall finds them
            query_vector=[1.0, 0.0, 0.0],
            embedding_model="fake-model",
        )
        return [hit.episode.episode_id for hit in hits]

    assert recalled("kind") == ["ep-kind-para"]
    assert recalled(SIMULATED) == ["ep-sim-para"]


# --- reuse: judged safe only for the same kind of target -----------------------------------


def test_reuse_can_be_judged_safe_only_for_the_same_kind_of_target(store: ExperienceStore) -> None:
    store.add(episode("ep-sim", SIMULATED_RUN), MemoryAccess.default())
    [hit] = store.search(query(), MemoryAccess.default())
    today = bad_release_on("checkout")

    # Without an environment the verdict is what it always was: same mechanism, service, fix.
    assert assess_reuse(hit, today, ["v2.4.0"]).safe_to_reuse
    assert assess_reuse(hit, today, ["v2.4.0"], environment=SIMULATED).safe_to_reuse
    # A caller on Kind holding this simulator hit (from anywhere) never gets "safe".
    on_kind = assess_reuse(hit, today, ["v2.4.0"], environment="kind")
    assert not on_kind.safe_to_reuse and on_kind.mechanism_match is True
    assert on_kind.contradicting == [
        "the past run acted on another kind of target (simulated, not kind)"
    ]
    assert on_kind.reason.endswith(
        "but the past run acted on another kind of target (simulated, not kind)"
    )


def test_an_episode_of_no_known_kind_is_never_safe_anywhere(store: ExperienceStore) -> None:
    hit_episode = episode("ep-nowhere", {"simulated": False})
    store.add(hit_episode, MemoryAccess.default())
    assert found(store, "kind") == [] and found(store, None) == []
    hit = MemoryHit(episode=hit_episode, similarity=1.0, outcome=EpisodeOutcome.SUCCESS)
    verdict = assess_reuse(hit, bad_release_on("checkout"), ["v2.4.0"], environment="kind")
    assert not verdict.safe_to_reuse
    assert (
        "the past run acted on another kind of target (unknown, not kind)" in verdict.contradicting
    )


def test_a_kind_rollback_is_safe_to_reuse_on_kind(store: ExperienceStore) -> None:
    store.add(episode("ep-kind", KIND_RUN), MemoryAccess.default())
    [hit] = store.search(query("kind"), MemoryAccess.default())
    assessed = assess_reuse(hit, bad_release_on("checkout"), ["v2.4.0"], environment="kind")
    assert assessed.safe_to_reuse and assessed.contradicting == []


# --- what the consolidator writes is what search reads ---------------------------------------


def test_a_consolidated_episode_is_found_only_by_runs_on_its_kind(store: ExperienceStore) -> None:
    record = make_record()
    built = {
        kind: Consolidator().build_episode(
            record=record,
            evidence=EVIDENCE,
            scenario=SCENARIO,
            partition=MemoryPartition.DEMO,
            simulated=kind == SIMULATED,
            environment_kind=kind,
        )
        for kind in (SIMULATED, "kind")
    }
    for made in built.values():
        assert store.add(made, MemoryAccess.default())
    assert built["kind"].provenance["environment"] == "kind"
    assert built["kind"].provenance["simulated"] is False

    incident = record.incident
    assert incident is not None and incident.environment == SIMULATED
    on_simulator = store.search(MemoryQuery.from_incident(incident), MemoryAccess.default())
    on_kind = store.search(
        MemoryQuery.from_incident(incident.model_copy(update={"environment": "kind"})),
        MemoryAccess.default(),
    )
    assert [h.episode.episode_id for h in on_simulator] == [built[SIMULATED].episode_id]
    assert [h.episode.episode_id for h in on_kind] == [built["kind"].episode_id]


# --- through the controller: the incident's environment reaches the search -------------------


class LiveLookingEnvironment(FakeEnvironment):
    """The tests' second adapter, reporting itself as a real target of kind "live"."""

    def describe(self) -> EnvironmentInfo:
        return super().describe().model_copy(update={"is_simulated": False, "kind": "live"})


def _plant(runtime: Runtime, planted: ExperienceEpisode) -> None:
    assert runtime.memory.add(planted, MemoryAccess.default())


@pytest.mark.e2e
async def test_a_run_on_a_live_target_reads_only_that_targets_memory(
    settings: Settings, tmp_path: Path
) -> None:
    """No controller change: the incident carries its environment kind, and the query that
    MemoryQuery.from_incident builds from it carries it on to the search."""
    live = settings.model_copy(
        update={
            "environment": EnvironmentConfig(
                kind="tests.unit.test_memory_environment:LiveLookingEnvironment",
                options={"state_dir": str(tmp_path / "live")},
            )
        }
    )
    runtime = Runtime(live)
    alert = ["checkout: HTTP 5xx ratio above 30%"]  # the fake target's alert, word for word
    _plant(runtime, episode("ep-simulated", SIMULATED_RUN, symptoms=alert))
    _plant(runtime, episode("ep-legacy", {}, symptoms=alert, created_at=T0 + timedelta(minutes=1)))
    live_run = {"simulated": False, "environment": "live"}
    _plant(runtime, episode("ep-live", live_run, symptoms=alert, created_at=T0 - timedelta(days=1)))

    record = await runtime.run_incident(
        "fake-bad-release",
        RunConfig(memory_write=False),
        approval_channel=OperatorApproves(runtime.approvals),
    )

    assert record.environment is not None and record.environment.kind == "live"
    assert [hit.episode.episode_id for hit in record.memory_hits] == ["ep-live"]
    [search] = [
        e for e in runtime.events.list_events(record.run_id) if e.category == "memory.search"
    ]
    assert [hit["episode_id"] for hit in search.payload["hits"]] == ["ep-live"]


@pytest.mark.e2e
async def test_the_simulators_memory_hits_are_unchanged_by_a_kind_episode(
    settings: Settings,
) -> None:
    """A simulator run writes its episode; a Kind copy of it, newer and word for word the
    same, is never a hit for the next simulator run, which finds exactly what it found."""
    runtime = Runtime(settings)
    approve = OperatorApproves(runtime.approvals)
    first = await runtime.run_incident("bad-deployment", RunConfig(), approval_channel=approve)
    assert first.state == IncidentState.RESOLVED and first.episode_id
    [written] = runtime.memory.list()
    assert written.provenance["environment"] == SIMULATED

    baseline = await runtime.run_incident(
        "bad-deployment", RunConfig(memory_write=False), approval_channel=approve
    )
    kind_copy = written.model_copy(
        update={
            "episode_id": "ep-kind-copy",
            "created_at": written.created_at + timedelta(minutes=1),
            "provenance": {**written.provenance, **KIND_RUN},
        }
    )
    _plant(runtime, kind_copy)
    again = await runtime.run_incident(
        "bad-deployment", RunConfig(memory_write=False), approval_channel=approve
    )

    def hits(record: Any) -> list[tuple[str, float, bool]]:
        return [(h.episode.episode_id, h.similarity, h.safe_to_reuse) for h in record.memory_hits]

    assert hits(baseline) == [(written.episode_id, 1.0, True)]
    assert hits(again) == hits(baseline)  # the Kind copy changed nothing
    assert again.state == baseline.state == IncidentState.RESOLVED
