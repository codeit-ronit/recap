"""Rollout rules on Kubernetes (recap/environments/kubernetes/rollout.py).

The story in tests/fixtures/kind/backend-snapshot.json (synthetic): v1.0.0 was revision 1,
a crashing v1.2.0 was revision 2 in an EARLIER run, an undo brought v1.0.0 back as revision 3
(its ReplicaSet reused, with its old creation time), a rolling restart made revision 4 (same
image), and v1.1.0 is rolling out as revision 5. The M5 audit's gotchas live here: a
ReplicaSet's creation time is not a deploy time, revisions from before the run's baseline
belong to other runs, a restart is not a release, and "complete" means really complete.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from recap.environments.kubernetes import rollout
from recap.environments.kubernetes.state import Baseline, ReleaseEntry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kind"
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def snapshot() -> dict[str, Any]:
    return json.loads((FIXTURES / "backend-snapshot.json").read_text(encoding="utf-8"))


def found(snapshot: dict[str, Any]) -> list[rollout.Revision]:
    return rollout.revisions(
        snapshot["deployment"], snapshot["replicasets"]["items"], "backend", "recap-demo"
    )


def test_revisions_are_the_deployments_own_replicasets_in_order(snapshot: dict) -> None:
    revisions = found(snapshot)

    # frontend's ReplicaSet and one without a revision number are not backend's history.
    assert [(r.number, r.version) for r in revisions] == [
        (2, "v1.2.0"),
        (3, "v1.0.0"),
        (4, "v1.0.0"),
        (5, "v1.1.0"),
    ]
    assert [r.reused for r in revisions] == [False, True, False, False]
    assert revisions[2].restarted_at == "2026-11-02T09:00:00Z"
    assert rollout.current(revisions) == revisions[-1]


# --- is the rollout done? ---------------------------------------------------------------------


def deployment(**status: Any) -> dict[str, Any]:
    complete = {
        "observedGeneration": 4,
        "replicas": 2,
        "updatedReplicas": 2,
        "readyReplicas": 2,
        "availableReplicas": 2,
        "conditions": [
            {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"}
        ],
    }
    return {
        "metadata": {"name": "backend", "generation": 4},
        "spec": {"replicas": 2},
        "status": {**complete, **status},
    }


def test_a_finished_rollout_is_complete() -> None:
    assert rollout.rollout_state(deployment()) == (True, "rollout complete")


@pytest.mark.parametrize(
    ("status", "why"),
    [
        ({"observedGeneration": 3}, "observe the latest spec"),
        ({"updatedReplicas": 1, "replicas": 3}, "1 of 2 replicas run the new template"),
        ({"replicas": 3}, "1 old replicas are still terminating"),  # a surge pod is left
        ({"availableReplicas": 1}, "1 of 2 updated replicas are available"),
        (
            {"conditions": [{"type": "Progressing", "reason": "ProgressDeadlineExceeded"}]},
            "progress deadline",
        ),
        ({"observedGeneration": None}, "observe the latest spec"),
    ],
    ids=["not-observed", "rolling", "surge-left", "unavailable", "deadline", "no-status"],
)
def test_anything_short_of_complete_is_not_complete(status: dict, why: str) -> None:
    complete, reason = rollout.rollout_state(deployment(**status))
    assert not complete and why in reason


def test_the_snapshot_mid_rollout_is_not_complete(snapshot: dict) -> None:
    complete, reason = rollout.rollout_state(snapshot["deployment"])
    assert not complete and "1 of 2 replicas run the new template" in reason


# --- releases: what get_recent_deployments shows ----------------------------------------------


def test_releases_since_the_baseline_fold_restarts_and_skip_older_runs(snapshot: dict) -> None:
    baseline = Baseline(version="v1.0.0", revision=3)
    log = [ReleaseEntry(NOW - timedelta(minutes=10), "backend", "v1.1.0", "v1.0.0", "release")]

    records = rollout.releases(
        "backend", found(snapshot), baseline=baseline, baseline_at=NOW - timedelta(hours=2), log=log
    )

    # Revision 4 is a restart of v1.0.0: not a release. v1.2.0 (revision 2) belongs to an
    # earlier run. v1.1.0's time is the run's own record of the change, not its ReplicaSet's.
    [release] = records
    assert (release.version, release.previous_version, release.status) == (
        "v1.1.0",
        "v1.0.0",
        "complete",
    )
    assert release.deployed_at == NOW - timedelta(minutes=10)
    assert release.change_summary == "release v1.1.0"


def test_a_restart_runs_the_same_release_on_a_new_replicaset(snapshot: dict) -> None:
    """Revision 4 restarted v1.0.0: not a release, but its pods run v1.0.0 (the verifier ties
    their events to that release)."""
    revisions = {revision.number: revision for revision in found(snapshot)}
    log = [
        ReleaseEntry(NOW - timedelta(minutes=50), "backend", "v1.2.0", "v1.0.0", "release"),
        ReleaseEntry(NOW - timedelta(minutes=40), "backend", "v1.0.0", "v1.2.0", "rollback"),
    ]

    records = rollout.releases(
        "backend",
        list(revisions.values()),
        baseline=Baseline(version="v1.0.0", revision=1),
        baseline_at=NOW - timedelta(hours=2),
        log=log,
    )

    [undo] = [record for record in records if record.version == "v1.0.0"]
    assert undo.replicaset == revisions[3].replicaset
    assert undo.restart_replicasets == [revisions[4].replicaset]
    assert all(not r.restart_replicasets for r in records if r.version != "v1.0.0")


def test_a_reused_replicaset_never_dates_a_release_by_its_old_creation_time(snapshot: dict) -> None:
    # The audit's case: run 2 releases v1.1.0 again; Kubernetes reuses run 1's ReplicaSet,
    # whose creation time is a day old. The release must be dated by run 2's own record.
    data = copy.deepcopy(snapshot)
    old = data["replicasets"]["items"][3]["metadata"]
    old["creationTimestamp"] = "2026-11-01T08:00:00Z"
    old["annotations"]["deployment.kubernetes.io/revision-history"] = "2"
    log = [ReleaseEntry(NOW - timedelta(minutes=3), "backend", "v1.1.0", "v1.0.0", "release")]

    [release] = rollout.releases(
        "backend",
        found(data),
        baseline=Baseline(version="v1.0.0", revision=4),
        baseline_at=NOW - timedelta(minutes=5),
        log=log,
    )

    assert release.deployed_at == NOW - timedelta(minutes=3)
    assert release.status == "complete"  # a release, even though its ReplicaSet was reused


def test_a_rollback_this_run_made_is_listed_as_one(snapshot: dict) -> None:
    data = copy.deepcopy(snapshot)
    # The undo of this run: v1.0.0's ReplicaSet (revision 4) becomes revision 6.
    restored = data["replicasets"]["items"][2]["metadata"]["annotations"]
    restored["deployment.kubernetes.io/revision"] = "6"
    restored["deployment.kubernetes.io/revision-history"] = "4"
    log = [
        ReleaseEntry(NOW - timedelta(minutes=10), "backend", "v1.1.0", "v1.0.0", "release"),
        ReleaseEntry(NOW - timedelta(minutes=2), "backend", "v1.0.0", "v1.1.0", "rollback"),
    ]

    records = rollout.releases(
        "backend",
        found(data),
        baseline=Baseline(version="v1.0.0", revision=3),
        baseline_at=NOW - timedelta(hours=2),
        log=log,
    )

    assert [(r.version, r.status, r.previous_version) for r in records] == [
        ("v1.0.0", "rollback", "v1.1.0"),
        ("v1.1.0", "complete", "v1.0.0"),
    ]
    assert records[0].deployed_at == NOW - timedelta(minutes=2)  # newest first


def test_a_change_made_outside_recap_is_dated_by_its_replicaset_or_pods(snapshot: dict) -> None:
    revisions = found(snapshot)
    baseline = Baseline(version="v1.0.0", revision=4)
    # Nobody in this run recorded v1.1.0: a ReplicaSet never reused is dated by its creation.
    [release] = rollout.releases(
        "backend", revisions, baseline=baseline, baseline_at=NOW - timedelta(hours=1), log=[]
    )
    assert release.deployed_at == datetime(2026, 11, 2, 9, 50, tzinfo=UTC)
    # A reused one is dated by its oldest pod, and never earlier than the baseline.
    reused = [r if r.number != 5 else _reused(r) for r in revisions]
    pod_times = {"backend-8e2c4f6a1b": NOW - timedelta(minutes=4)}
    [release] = rollout.releases(
        "backend",
        reused,
        baseline=baseline,
        baseline_at=NOW - timedelta(minutes=30),
        log=[],
        pod_times=pod_times,
    )
    assert release.deployed_at == NOW - timedelta(minutes=4) and release.status == "rollback"


def _reused(revision: rollout.Revision) -> rollout.Revision:
    return rollout.Revision(**{**revision.__dict__, "reused": True})


# --- rollback plans and the revision a rollback restores -------------------------------------


def test_the_plan_names_the_most_recent_earlier_image_with_its_revision(snapshot: dict) -> None:
    plan = rollout.rollback_plan("backend", found(snapshot), Baseline("v1.0.0", 3))

    assert (plan.possible, plan.current_version, plan.target_version) == (True, "v1.1.0", "v1.0.0")
    assert "revision 4 (v1.0.0)" in plan.reason and "revision 5" in plan.reason


def test_the_plan_never_names_an_image_from_before_the_runs_baseline(snapshot: dict) -> None:
    # This run's baseline is the restarted v1.0.0 (revision 4) and nothing was released since:
    # only the crashing v1.2.0 of an earlier run differs, and it must not be offered.
    data = copy.deepcopy(snapshot)
    data["replicasets"]["items"].pop(3)  # no v1.1.0
    revisions = found(data)

    plan = rollout.rollback_plan("backend", revisions, Baseline("v1.0.0", 4))
    unscoped = rollout.rollback_plan("backend", revisions, None)

    assert not plan.possible and plan.target_version is None
    assert "since this run's baseline" in plan.reason
    assert unscoped.target_version == "v1.2.0"  # what an unscoped history would have offered


def test_a_rollback_resolves_the_highest_revision_of_the_approved_version(snapshot: dict) -> None:
    revisions = found(snapshot)
    assert rollout.revision_for(revisions, "v1.0.0").number == 4
    with pytest.raises(rollout.RolloutError, match="no revision runs v9.9.9"):
        rollout.revision_for(revisions, "v9.9.9")
    with pytest.raises(rollout.RolloutError, match="already the running revision"):
        rollout.revision_for(revisions, "v1.1.0")


def test_no_history_means_no_plan() -> None:
    plan = rollout.rollback_plan("backend", [], None)
    assert not plan.possible and plan.current_version == "unknown"
