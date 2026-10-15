# ADR 0012: Platform signals outrank log text in the evidence verifier

**In one sentence:** when what a log line *says* disagrees with what the platform *measures*
(health snapshots, metrics, cluster events), the verifier believes the platform, records the
conflict with the evidence IDs of both sides (rule V6), and never lets log text alone prove
a root cause.

## Status

Accepted (M1, 2026-10-15). Fixes audit finding 2.2.

## Context

A log line is text that a buggy library, a compromised service or a user can write. In V1,
rule V3 marked memory pressure SUPPORTED as soon as any log line contained
`OutOfMemoryError`, before its own contradiction check ran, and rule V2 cleared a bad
release whenever half the service's errors said "upstream D call failed", without checking
that D was failing. With forged lines in both places RECAP restarted the healthy `payment`
service (an auto-approved but wrongful write) and never fixed the real bad release.

## Decision

In [recap/agents/verifier.py](../../recap/agents/verifier.py):

- The verifier parses health snapshots. A service is **observed healthy** only if it has at
  least one health snapshot or error-rate series and every one of them looks healthy
  (Running, all replicas ready, error rate at or below 5%).
- **V3:** OOM kills or memory at or above 90% of the limit support resource pressure; OOM
  log lines only corroborate. Text alone is INSUFFICIENT. Text against low memory with
  quiet events, or against an observed-healthy service, is CONTRADICTED.
- **V2:** blame toward an observed-healthy dependency is not counted, and the check says so.
- **V4:** a dependency failure on an observed-healthy dependency is CONTRADICTED.
- The ranking's "deepest root cause" graph ignores blame toward observed-healthy services.
- **V6-conflict-resolution** is the rule ID that records each such resolution, with the
  evidence on both sides. Statuses stay supported / contradicted / insufficient: never
  "proven".

## Consequences

### Good

- The forged-log scenario now ends RESOLVED by rolling back the real bad release, and no
  write reaches `payment`; both rule-level reproductions pass.
- Guard tests keep the fix narrow: real OOM kills still prove pressure, and blame toward a
  dependency that really fails still clears the caller.

### Bad

- A service whose only sign of memory trouble is log text now needs another investigation
  step (a memory metric or events) before RECAP acts. That can cost tool calls.
- "Observed healthy" is a fixed threshold rule; a service that is degraded but under 5%
  errors counts as healthy.
- Platform signals can be wrong or forged too (a compromised metrics pipeline); V6 only
  ranks them above free text.

## Alternatives considered

- **Reorder V3 only (contradiction check first).** Not enough: in the reproduction the
  investigator had not read the memory metric or events yet, so there was nothing to
  contradict with.
- **Ignore log text entirely.** Logs carry the version tags and dependency names that the
  other rules need.
