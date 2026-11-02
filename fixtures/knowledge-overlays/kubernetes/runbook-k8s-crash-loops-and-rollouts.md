# Runbook: Kubernetes rollouts that stall and pods that crash loop

A rollout replaces a Deployment's pods with pods from a new ReplicaSet. When the new pods
cannot start, or start and never become ready, the rollout stalls. With a cautious update
strategy the old pods keep serving, so the service can look only partly broken. Which
back-off the new pods are in tells you where to look first.

## Symptoms

- `CrashLoopBackOff`: a container starts, exits, and the kubelet waits longer before each
  restart. The restarts metric climbs and ready replicas stay below desired.
- `ImagePullBackOff` or `ErrImagePull`: the node cannot pull the image. The pod is stuck
  before its container starts, so it writes no logs at all.
- A rollout that is stuck: fewer updated replicas than desired and, once the progress
  deadline has passed, the Deployment condition `ProgressDeadlineExceeded`.
- Errors that begin when the rollout begins, on the service being rolled out.

## Diagnosis: crash loop or image pull

1. Call `get_k8s_like_events` for the service. `CrashLoopBackOff` means the code starts and
   exits; `BackOff` after an `OOMKilled` termination means memory (see the readiness and
   memory runbook); `ImagePullBackOff` means the image itself.
2. For a crash loop, read `get_service_logs`: the last lines before each exit name the
   error. An exception tagged with a release, such as `[build <release>]` or
   `(version=<release>)`, points at the code of that release.
3. For an image-pull back-off the logs are empty. Compare the newest revision with the one
   before it in `get_recent_deployments`.
4. Compare times with `get_recent_deployments` and the `error_rate` metric. A crash loop
   that begins within minutes of the service's own release makes that release a suspect. A
   crash loop on a service that was not released points elsewhere: memory, configuration, or
   a dependency it needs at start-up.

## What a stalled rollout means for the fix

New pods that crash loop or cannot pull their image right after a release point at that
release: the earlier revision's pods are what still serves. Rolling back to the earlier
release, after the `propose_rollback` dry run names it, replaces the failing pods and lets
the rollout finish. A rolling restart does not help: restarted pods use the same template,
the same image and the same defect.

## Verification

- `get_service_health` shows every replica ready and the status `Running`.
- No new `CrashLoopBackOff`, `BackOff` or `ImagePullBackOff` events for the service.
- The restarts metric stops climbing and the error rate falls under its threshold.
