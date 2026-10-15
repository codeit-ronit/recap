# Runbook: memory pressure and crash loops

Memory pressure means a container is using nearly all of its memory limit. When it
crosses the limit it is killed and restarted, and if the leak continues the pod ends up
in a crash loop.

## Symptoms

- `OOMKilled` events: a container exceeded its memory limit and was killed.
- `BackOff` events and a `CrashLoopBackOff` status while the pod keeps restarting.
- Readiness probe failures, so fewer replicas are ready than desired.
- The memory metric climbs toward the limit; logs show heap warnings and
  `OutOfMemoryError` lines.
- The error rate rises because the remaining replicas take all the traffic.

## Diagnosis

1. Compare the memory_mb metric with the container memory limit. Usage at or above 90%
   of the limit is memory pressure; usage that stays below about 70% argues against it.
2. Check the restarts metric and look for OOMKilled and BackOff events.
3. Tell it apart from a bad release: a leak grows over a long time, it is not tied to a
   deployment, and there are no new exception types tagged with a release version.

## Remediation

Mechanism: resource_pressure
Recommended action: restart_service

A rolling restart replaces pods one at a time, so the service keeps serving traffic. It
is reversible and safe when the service has at least two replicas, and it still needs an
approval bound to the exact service and incident. A restart does NOT fix a bad release:
the new pods run the same broken code and fail again.

## Verification

- Memory drops back to its normal baseline after the restart.
- No new OOMKilled or BackOff events appear, and all replicas are ready.
- The error rate falls back under the SLO.

## Follow-up

A restart buys time; it does not remove the leak. Watch memory for the next hours,
capture a heap profile, and fix the code or raise the limit through a normal reviewed
change.
