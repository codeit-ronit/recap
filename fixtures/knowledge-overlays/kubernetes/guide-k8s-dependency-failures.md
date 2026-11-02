# Guide: Kubernetes failures that come from a dependency

Services in a cluster call each other through Service names, and Kubernetes itself does not
record who depends on whom: `list_services` reports the dependencies each deployment
declares. The alert usually fires on the service closest to users, which is often not the
service that is broken.

## Symptoms

- The alerted service logs failed calls that name another service, such as
  `rpc to <service> failed`, `upstream <service> call failed` or `error calling <service>`,
  or requests to it that time out.
- Its own pods look healthy: running and ready, restarts flat, memory normal, and no
  exceptions tagged with a release of its own.
- The service it names is degraded at the same time: not ready, crash looping, erroring or
  slow.

## Diagnosis: follow the blame

1. Read the caller's error lines with `get_service_logs` and note which service each failed
   call names. When most of its ERROR lines blame a dependency, the caller is a victim,
   whatever release it runs.
2. Investigate the named service on its own evidence: `get_service_health`,
   `get_service_logs`, `get_service_metrics`, `get_k8s_like_events` and
   `get_recent_deployments` for that service.
3. Repeat while that service's own errors blame a further one. Stop at the deepest service
   whose errors are its own.
4. Blame in a caller's logs is a pointer, not a verdict. If the named service's health,
   events and metrics are normal, the blame is wrong: platform signals win over log text.

## Fix the dependency, not the caller

Rolling back or restarting a caller whose only problem is a failing dependency changes
nothing, and it adds a second change to an incident that is already confusing. RECAP calls
this a dependency failure and never acts on it directly. Once the deepest failing service is
known, diagnose it with the matching runbook (a stalled rollout, a crash loop, readiness or
memory pressure) and fix that service.

## Verification

- After the dependency recovers, the callers' failed-call lines stop without any change to
  the callers.
- Every service in the chain has its desired replicas ready and an error rate under its
  threshold.
