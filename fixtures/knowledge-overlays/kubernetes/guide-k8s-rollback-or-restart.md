# Guide: rollback, rolling restart, or a person

RECAP can change a Kubernetes service in two ways only: `execute_rollback` to a named
earlier release, and `restart_service`, a rolling restart. Both are reversible and both need
a person's approval of the exact change. The choice follows from the diagnosis, never from
the symptom alone: a crash loop can come from a release, from memory or from a dependency.

## When a rollback fits

- The service's own errors, such as exceptions tagged with its release or new pods that
  crash loop, begin within minutes of its latest release: from about 5 minutes before it to
  about 30 minutes after it.
- Run `propose_rollback` first. The dry run names the revision and the release a rollback
  would restore, and changes nothing.
- Roll back only to a release the evidence names as healthy, the one that ran before the
  suspect release; never to the suspect release itself or to the one already running.
- Kubernetes renumbers revisions when it rolls back, so an approval names the release, and
  the revision is looked up again right before the change.

## When a rolling restart fits

- Memory pressure or a stuck process on the service itself, shown by platform signals:
  memory at the limit, `OOMKilled` terminations, readiness failing. No release explains the
  errors.
- The service runs at least two replicas, so pods are replaced one at a time and it keeps
  serving.
- A restart does not fix a bad release: the new pods run the same image and fail again.

## When neither is safe: hand over to a person

- The evidence does not settle the cause: two root causes are level, or the alerted service
  only repeats a dependency's errors and nothing below it explains them.
- The fix would need anything else: scaling, editing configuration or secrets, a new image,
  or deleting anything.
- No healthy earlier release exists, or the earlier release failed too.
- A change was already made and the service did not recover.

RECAP then stops, reports what it found and what it ruled out, and a person decides.

## After the change

- A rollout takes time. Wait until every replica is updated and ready and no old pod is
  left; one probe right after the change proves nothing.
- Recovery is judged by RECAP's independent health check, never by quieter logs.
