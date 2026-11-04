"""Rollouts on Kubernetes: revisions, the rollout-complete rule, releases and rollback plans.

A Deployment's history lives in its ReplicaSets: each pod-template change makes (or reuses)
one, numbered by its `deployment.kubernetes.io/revision` annotation. Three facts about that
history shape the rules below (docs/m5-integration-plan.md, 3.5, and the M5 audit):

* A ReplicaSet's creationTimestamp is NOT when its revision went live. `kubectl rollout
  undo`, and `set image` back to an earlier template, reuse the old ReplicaSet with its old
  timestamp and give it a new revision number. So a release's `deployed_at` comes from the
  run's own record of the change it made (the release log), and only otherwise from the
  ReplicaSet (when it was never reused) or its pods.
* History survives across runs (revisionHistoryLimit keeps ten). Revisions from before this
  run's baseline belong to earlier runs: they are left out of the deployment list and out of
  rollback plans, so an image from another run can never look like this incident's release
  or its restore point.
* A rolling restart makes a new revision with the SAME image. It is not a release: it is
  folded into the release it restarted, so it never reads as a deployment of a healthy
  version minutes before the alert.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from recap.core.observations import DeploymentRecord, RollbackPlan
from recap.environments.kubernetes.state import Baseline, ReleaseEntry
from recap.environments.kubernetes.translate import (
    UNKNOWN_VERSION,
    ReleaseFormat,
    annotations_of,
    created_at,
    image_of,
    name_of,
    template_of,
    version_of,
)

REVISION = "deployment.kubernetes.io/revision"
REVISION_HISTORY = "deployment.kubernetes.io/revision-history"
CHANGE_CAUSE = "kubernetes.io/change-cause"
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"


class RolloutError(RuntimeError):
    """A rollout question has no safe answer (no such revision, already running it, ...)."""


@dataclass(frozen=True)
class Revision:
    """One ReplicaSet of a Deployment, as a numbered revision."""

    number: int
    replicaset: str
    image: str | None
    version: str | None
    created_at: datetime | None
    reused: bool  # brought back by an undo (or a set image to an older template)
    change_cause: str
    restarted_at: str | None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _uid(obj: Mapping[str, Any]) -> str:
    metadata = obj.get("metadata")
    return str(metadata.get("uid") or "") if isinstance(metadata, dict) else ""


def owned_by(replicaset: Mapping[str, Any], deployment: Mapping[str, Any]) -> bool:
    """True if the ReplicaSet belongs to the Deployment (owner reference, by uid)."""
    uid = _uid(deployment)
    metadata = replicaset.get("metadata")
    owners = metadata.get("ownerReferences") if isinstance(metadata, dict) else None
    if not uid or not isinstance(owners, list):
        return False
    return any(
        isinstance(owner, dict) and owner.get("kind") == "Deployment" and owner.get("uid") == uid
        for owner in owners
    )


def revisions(
    deployment: Mapping[str, Any],
    replicasets: Sequence[Mapping[str, Any]],
    service: str,
    repository: str,
    release_format: ReleaseFormat = "semver",
) -> list[Revision]:
    """The Deployment's revisions, oldest first. ReplicaSets it does not own are ignored."""
    found: list[Revision] = []
    for replicaset in replicasets:
        if not owned_by(replicaset, deployment):
            continue
        annotations = annotations_of(replicaset)
        raw = annotations.get(REVISION, "")
        if not raw.isdigit() or int(raw) < 1:
            continue
        image = image_of(replicaset, service, repository)
        template_annotations = _dict(
            _dict(template_of(replicaset).get("metadata")).get("annotations")
        )
        restarted = template_annotations.get(RESTARTED_AT)
        found.append(
            Revision(
                number=int(raw),
                replicaset=name_of(replicaset),
                image=image,
                version=version_of(image, release_format),
                created_at=created_at(replicaset),
                reused=bool(annotations.get(REVISION_HISTORY)),
                change_cause=annotations.get(CHANGE_CAUSE, ""),
                restarted_at=str(restarted) if restarted else None,
            )
        )
    return sorted(found, key=lambda revision: revision.number)


def current(found: Sequence[Revision]) -> Revision | None:
    """The live revision: the highest number (a new template always gets the next one)."""
    return found[-1] if found else None


# --- is the rollout done? -------------------------------------------------------------------


def _count(status: Mapping[str, Any], key: str) -> int:
    value = status.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def progress_deadline_exceeded(deployment: Mapping[str, Any]) -> bool:
    conditions = _dict(deployment.get("status")).get("conditions")
    return any(
        _dict(c).get("type") == "Progressing"
        and _dict(c).get("reason") == "ProgressDeadlineExceeded"
        for c in (conditions if isinstance(conditions, list) else [])
    )


def rollout_state(deployment: Mapping[str, Any]) -> tuple[bool, str]:
    """(complete, why): the checks `kubectl rollout status` makes, made strict.

    Complete means: the controller has observed the latest spec (observedGeneration caught up
    with generation), the rollout did not exceed its progress deadline, every desired replica
    runs the new template (updatedReplicas), no old pod is left (status.replicas equals
    updatedReplicas: surge pods count), and every updated replica is available.
    """
    metadata = _dict(deployment.get("metadata"))
    status = _dict(deployment.get("status"))
    spec = _dict(deployment.get("spec"))
    generation = metadata.get("generation")
    observed = status.get("observedGeneration")
    if not isinstance(generation, int) or not isinstance(observed, int) or observed < generation:
        return False, "waiting for the controller to observe the latest spec"
    if progress_deadline_exceeded(deployment):
        return False, "the rollout exceeded its progress deadline"
    desired = spec.get("replicas", 1)
    desired = desired if isinstance(desired, int) and not isinstance(desired, bool) else 1
    updated = _count(status, "updatedReplicas")
    replicas = _count(status, "replicas")
    available = _count(status, "availableReplicas")
    if updated != desired:
        return False, f"{updated} of {desired} replicas run the new template"
    if replicas != updated:
        return False, f"{replicas - updated} old replicas are still terminating"
    if available != updated:
        return False, f"{available} of {updated} updated replicas are available"
    return True, "rollout complete"


# --- releases (get_recent_deployments) ------------------------------------------------------


def releases(
    service: str,
    found: Sequence[Revision],
    *,
    baseline: Baseline | None,
    baseline_at: datetime | None,
    log: Sequence[ReleaseEntry],
    pod_times: Mapping[str, datetime] | None = None,
) -> list[DeploymentRecord]:
    """The image changes of `service` since this run's baseline, newest first.

    Revisions up to the baseline revision are history from earlier runs and are left out; a
    restart-only revision (same image as the one before it) is not a release, but its
    ReplicaSet runs the release (restart_replicasets). deployed_at is
    the time the run's release log recorded the change; without such an entry (a change made
    outside RECAP), the ReplicaSet's creation time if it was never reused, else the earliest
    creation time of its pods, never earlier than the baseline.
    """
    previous = baseline.version if baseline else None
    after = [r for r in found if baseline is None or r.number > baseline.revision]
    unused = [entry for entry in log if entry.service == service and entry.kind != "restart"]
    records: list[DeploymentRecord] = []
    for revision in after:
        version = revision.version or UNKNOWN_VERSION
        if version == previous:  # a rolling restart: the same release, on a new ReplicaSet
            if records and records[-1].version == version:
                records[-1].restart_replicasets.append(revision.replicaset)
            continue
        entry = next((e for e in unused if e.version == version), None)
        if entry is not None:
            unused.remove(entry)
        deployed_at = _became_current(revision, entry, pod_times or {})
        if baseline_at is not None and (deployed_at is None or deployed_at < baseline_at):
            deployed_at = baseline_at
        if deployed_at is None:
            continue  # nothing says when it went live: better left out than misdated
        status = (
            "rollback" if (entry.kind == "rollback" if entry else revision.reused) else "complete"
        )
        records.append(
            DeploymentRecord(
                service=service,
                version=version,
                previous_version=(entry.previous_version if entry else None) or previous,
                replicaset=revision.replicaset,
                deployed_at=deployed_at,
                status=status,
                change_summary=revision.change_cause,
            )
        )
        previous = version
    return list(reversed(records))


def _became_current(
    revision: Revision, entry: ReleaseEntry | None, pod_times: Mapping[str, datetime]
) -> datetime | None:
    if entry is not None:
        return entry.at
    if not revision.reused and revision.created_at is not None:
        return revision.created_at
    return pod_times.get(revision.replicaset)


# --- rollbacks ------------------------------------------------------------------------------


def rollback_plan(
    service: str, found: Sequence[Revision], baseline: Baseline | None
) -> RollbackPlan:
    """Dry run: the most recent earlier revision (since the baseline) with another image."""
    live = current(found)
    if live is None:
        return RollbackPlan(
            service=service,
            current_version=UNKNOWN_VERSION,
            possible=False,
            reason=f"{service} has no revision history to roll back to",
        )
    running = live.version or UNKNOWN_VERSION
    floor = baseline.revision if baseline else 0
    earlier = [
        r
        for r in found
        if floor <= r.number < live.number and r.version and r.version != live.version
    ]
    if not earlier:
        return RollbackPlan(
            service=service,
            current_version=running,
            possible=False,
            reason=(
                f"no earlier revision of {service} since this run's baseline runs another image "
                f"than {running} (revision {live.number})"
            ),
        )
    target = max(earlier, key=lambda r: r.number)
    return RollbackPlan(
        service=service,
        current_version=running,
        target_version=target.version,
        possible=True,
        reason=(
            f"revision {target.number} ({target.version}) is the most recent earlier revision "
            f"with another image; {service} runs {running} at revision {live.number}"
        ),
    )


def revision_for(found: Sequence[Revision], version: str) -> Revision:
    """The revision to roll back to for an approved version: the highest whose image is
    exactly that version. The approval binds the version, never a revision number, because
    an undo renumbers the revision it restores (docs/m5-integration-plan.md, 3.5)."""
    live = current(found)
    matching = [r for r in found if r.version == version]
    if not matching:
        raise RolloutError(f"no revision runs {version}")
    target = max(matching, key=lambda r: r.number)
    if live is not None and target.number == live.number:
        raise RolloutError(f"{version} is already the running revision ({live.number})")
    return target
