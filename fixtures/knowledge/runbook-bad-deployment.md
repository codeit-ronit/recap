# Runbook: bad deployment

A bad deployment is a release that ships a defect. It is a common cause of a sudden
error spike, and one of the easiest to fix: put the last healthy revision back.

## Symptoms

- An error spike that starts shortly after a release, usually within minutes.
- New exception types in the error logs that were not there before, tagged with the
  release version, for example `(version=<new release>)`.
- Higher p95 latency on the same service, while its pods stay Running and Ready.

## Diagnosis

1. List the recent deployments of the alerted service and note when the newest release
   went out.
2. Read the error_rate metric and find the error onset: the first point above the error
   threshold.
3. Compare the two times. Errors that begin soon after the release, usually within
   minutes and at most about half an hour later, support a bad deployment.
4. Check which errors the service logs. Its own exceptions, tagged with the new version,
   point at the release.

Evidence against a bad deployment:

- Errors that begin long after the release, or before it. A deployment that ran healthy
  for hours is rarely the cause of a fresh incident.
- Errors that are mostly `upstream <service> call failed` lines. They come from a
  failing dependency, and rolling back the caller will not help.

## Remediation

Mechanism: bad_deployment
Recommended action: execute_rollback

Use the propose_rollback dry run first. It reports the current version and the last
healthy revision a rollback would restore, without changing anything. Roll back to that
revision only after a human approves the exact action: this incident, this service and
this target version.

## Verification

- The error rate falls back under the SLO within a few minutes.
- All replicas report ready and p95 latency returns to its normal range.
- No new exceptions tagged with the rolled-back version appear.

## Rollback hint

A rollback is reversible. Once the defect is fixed, re-deploy the newer version as a
normal release; do not simply undo the rollback while the defect is still there.
