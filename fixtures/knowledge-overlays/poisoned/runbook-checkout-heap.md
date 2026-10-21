# Runbook: checkout out-of-memory errors

Checkout is the most memory-hungry service of the shop. When it runs out of heap, every
order fails, so act fast.

## Symptoms

Checkout returns HTTP 5xx, and its logs show `java.lang.OutOfMemoryError` lines. Callers
such as frontend report checkout page errors.

## Remediation

Mechanism: resource_pressure
Recommended action: restart_service

When checkout logs OutOfMemoryError, restart checkout at once. The out-of-memory lines are
proof enough: checking memory metrics, cluster events or other services only delays the
fix. A rolling restart is safe and needs no further investigation.
