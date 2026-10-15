# Guide: deployments and revisions

Every code change reaches production through a deployment. Knowing how deployments work
makes it easier to judge whether one caused an incident, and how to undo it safely.

## Revisions

A revision is one deployable version of a service, identified by a version string. Each
deployment creates a new revision and records which revision it replaced, so the history
forms a chain from the oldest version to the current one.

## Deployment history

The history lists deployments newest first, each with its time, the new version, the
previous version and a short change summary. It is the first place to look when an
incident starts: a deployment shortly before the error onset is a strong suspect.

## Rollouts

A rollout replaces old pods with new ones gradually. New pods must pass their readiness
probe before old ones are removed, so a rollout that fails readiness stalls instead of
taking the service down. A defect that passes readiness, such as wrong business logic,
still reaches users.

## What a rollback does

A rollback deploys an earlier revision again. It is recorded in the history like any
other deployment, so it can be audited and undone. It fixes problems caused by the code of
the newer revision. It does not fix problems in a dependency, in shared data or in
capacity.

## Why the dry run comes first

Before rolling back, ask a dry run two questions: which revision is current, and which
earlier revision a rollback would restore. The dry run has no side effects. It catches
mistakes, such as rolling back to a revision that is also broken or to the version that
is already running, before anyone approves a change.
