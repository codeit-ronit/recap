# Runbook: Kubernetes readiness failures and memory pressure

A readiness probe decides whether a pod receives traffic. A pod that fails it keeps running
but is taken out of its Service's endpoints, so a service can show `Running` while too few
replicas are ready to carry its load. Memory pressure is a common reason; a dependency that
the probe checks is another.

## Symptoms

- Ready replicas below desired while the pods keep running, with `Unhealthy` events
  (readiness probe failed).
- Memory near the container limit: `memory_mb` from `get_service_metrics` at or above 90% of
  the limit.
- `OOMKilled` terminations, read from the pod's status, and `BackOff` events once the kernel
  kills a container that crosses its limit.
- Errors on the service and on its callers, because fewer ready pods take all the traffic.

## Diagnosis

1. `get_service_health`: ready against desired replicas, the status and the error rate.
2. `get_service_metrics` with `memory_mb`: compare usage with the container limit, not with
   yesterday. At or above 90% of the limit is memory pressure; below about 70% argues
   against it.
3. `get_service_metrics` with `restarts`, and `get_k8s_like_events`: look for `OOMKilled`,
   `BackOff` and `Unhealthy`.
4. `get_recent_deployments`: memory that fills over time with no release in between points
   at a leak in code that has run for a while. Errors that start right after a release, and
   are tagged with it, point at the release instead.
5. Platform signals outrank log text: an out-of-memory line in the logs while memory stays
   low and no container was killed is not memory pressure.

## Rolling restart for memory pressure

When memory pressure on the service itself is confirmed, a rolling restart replaces its pods
one at a time and frees the memory each of them held. It is safe when the service runs at
least two replicas and its update strategy keeps a ready pod serving while each one is
replaced; when that is not so, hand the decision to a person. A restart buys time: the code
that filled memory is still there, so watch memory afterwards and fix the cause through a
normal reviewed change.

## Verification

- Ready replicas equal desired, and no new `Unhealthy`, `OOMKilled` or `BackOff` events.
- Memory is back near its usual level and the error rate is under its threshold.
