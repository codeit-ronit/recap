# Guide: cascading failures across services

When services call each other, one failing dependency can make many callers fail. The
alert usually fires on a caller, because callers sit closer to users. Changing the wrong
service wastes time and adds risk, so find the source of the failure first.

## Symptoms

- Several services degrade at about the same time.
- The alerted service logs `upstream <service> call failed` errors: its calls to a
  dependency time out or return errors.
- The caller's own pods look healthy: Running and Ready, no OOMKilled events and no new
  exceptions tagged with a release version.

## Diagnosis: follow the upstream errors

1. Read the caller's error logs and note which dependency each upstream error names.
2. Move to that dependency and read its logs, health and recent changes.
3. Repeat while the dependency itself reports upstream call failed errors. Stop at the
   deepest failing dependency: the first service whose errors are its own.
4. Diagnose that service with the matching runbook, such as bad deployment or memory
   pressure.

## Remediation

Mechanism: dependency_failure

Fix the dependency, not the caller. Rolling back or restarting a caller whose only
problem is a failing dependency changes nothing, and it adds a second change to an
incident that is already confusing.

This guide deliberately gives no direct remediation line for this mechanism, because a
dependency failure describes a symptom, not a root cause. Once the deepest failing
dependency is known, apply the remediation for its own mechanism.

## Verification

- After the dependency recovers, the callers' upstream errors stop without any change to
  the callers.
- Every service in the chain returns under its SLO.
