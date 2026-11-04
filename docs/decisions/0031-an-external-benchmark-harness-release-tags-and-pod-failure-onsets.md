# ADR 0031: RECAP on an external benchmark's cluster: the harness owns the fault, a release is an image tag, and pod failures can date an onset

**In one sentence:** to work a SREGym problem honestly, the Kubernetes adapter gains an
external-harness mode (the benchmark deployed the app and injected the fault, so RECAP's reset
changes nothing), a release may be any image tag (a key into the Deployment's own revision
history, never an image RECAP composes), a target without request metrics says "not measured"
instead of guessing, and the verifier may date a bad release's onset by the first warning event
that says the service's own pods fail.

## Status

Accepted (M5 W5, 2026-11-04). Builds on [ADR 0007](0007-replaceable-environment-adapter.md)
(one adapter per target), [ADR 0014](0014-signed-capabilities-at-the-write-boundary.md)
(the adapter verifies every write's capability), [ADR 0015](0015-operator-auth-and-no-wins-approvals.md)
(a person's exact approval) and the M5 integration plan
([docs/m5-integration-plan.md](../m5-integration-plan.md)); SREGym is pinned at release 1.1,
commit `a90b43cc973158120119a9f388081eac4f7adb36`.

## Context

The Kind demo cluster (W2 to W4) is RECAP's own: RECAP restores its baseline, injects each fault
as a typed step, tags every image `vX.Y.Z`, probes every service over HTTP and reads Prometheus.
A benchmark is different by design. SREGym's external-harness mode deploys an application and
injects one of its problems, then leaves the cluster to the agent; its own executable mitigation
oracle grades the state afterwards. For the problem chosen for M5 (`misconfig_app_hotel_res`:
the geo service of the hotel-reservation app is moved to a release that crashes at start-up),
four of RECAP's assumptions did not hold:

- **Who injects the fault.** RECAP's reset would have "restored" a baseline it does not own
  and needed the recap-scenario identity, which has no job here.
- **What a release is called.** The images are `ghcr.io/sregym/hotel-reservation:<tag>@sha256:…`
  with date and commit tags. RECAP read only `vX.Y.Z` tags and refused digests, so it saw no
  release, found no restore point, and its tool validation would refuse the fix.
- **What is measured.** The app exports no request metrics and has no NodePort to probe. The
  health tool needed an error rate and a latency, and the oracle counted an unprobed service
  as failing every request.
- **When the errors began.** The verifier's rule for a bad deployment (V2) dates the onset by
  an `error_rate` metric or by ERROR lines tagged with the release. A crash-looping Go service
  logging "no reachable servers" has neither, so a correct diagnosis could never be supported.

## Decision

1. **The harness owns the fault** (`harness: external`). Reset injects and restores nothing. It
   checks that the allowlisted Deployments exist, takes each one's first revision as the run's
   baseline (the harness deploys the app afresh for every problem), starts the observation
   window before the app's deploy, reads only as recap-reader and recap-oracle, and waits for
   the alert from the cluster's own state (crash loops, unready replicas). Scenario files say
   so with an `external` block (harness, pinned release and commit, the problem id): evaluation
   provenance, like `expected`, that never reaches an agent. The SREGym profile documents the
   app's call graph for the deployments that carry no depends-on annotation: the published
   architecture, nothing about the fault.
2. **A release may be an image tag** (`release_format: tag`, argument type `release`). It is the
   image's tag, also behind a digest, checked by the tag grammar. It is still only a key: an
   approved rollback restores the image that the named revision ran, and the read-back confirms
   exactly that image, digest included. The demo cluster keeps `vX.Y.Z`. Trusted text that
   repeats a release (the verifier's open questions) still repeats only `vX.Y.Z` shapes. One
   place puts tags read from tool data into trusted text: the remediation proposal's schema,
   whose `to_version` is an enum of the restore points the evidence names (VP4's own list). A
   value enters that enum only if it passes the tag grammar, as the registry would require of
   the argument anyway.
3. **What is not measured is said so.** `HealthSnapshot.error_rate` and `latency_p95_ms`, and
   the oracle's `GroundTruthHealth.error_rate`, may be None: "not measured on this target". The
   verifier then judges by status and readiness alone, a metric RECAP cannot read is refused
   with that reason, and nothing is estimated in its place. The oracle asks one thing more of a
   service it cannot probe: every container has run a minute, and none exited in the last five
   (the kubelet's longest crash back-off). Without a readiness probe, a crash-looping container
   is Running and Ready for the seconds each start lasts, and status and readiness alone would
   call that a recovery.
4. **Pod failures can date a bad release's onset.** When a service has no `error_rate` metric
   and no release-tagged ERROR line, V2 takes the first Warning event that says its own pods
   fail (CrashLoopBackOff, BackOff, Unhealthy, ImagePullBackOff, ErrImagePull), dated by its
   first occurrence (`ClusterEvent.first_seen`: an aggregated "Back-off ... x20" began long
   before its latest one). The deploy-timing window (-5..+30 min) and the upstream-dominance
   check apply unchanged; a metric, then tagged logs, keep precedence wherever they exist.

## Consequences

- A SREGym run is judged twice, and the two are never merged: RECAP's oracle (rollout,
  readiness) decides RECAP's own RESOLVED, and SREGym's mitigation oracle (`run-oracle.py`,
  exit 0 = pass) is the benchmark's verdict, recorded next to it. No LLM judge is used.
- A person approves every write, as on the demo cluster, so these runs are human-in-the-loop
  and not comparable with SREGym's leaderboard; they say so.
- A commit tag is long (`sha-` and 40 hex digits), and the release the diagnosis blames is a
  tag the evidence names too. The first SREGym run proposed rolling geo back to the very release
  that broke it, and VP4 blocked it (run-e08c17d46e, docs/m5-progress.md). Since then the
  proposal schema offers only the restore points the evidence names, so the model picks one
  instead of copying a tag. VP4 still checks the answer, and a garbled tag blocks the run safely
  instead of rolling back to the wrong revision.
- The fake cluster seeds such a target offline (`tests/fixtures/sregym`, synthetic): a crashing
  image that a harness rolled out serves with crash-looping pods, as without a readiness probe.

## Alternatives considered

- **Run RECAP as SREGym's containerized agent with its kubectl passthrough or shell tools.**
  Rejected: free-form, cluster-wide writes; RECAP's guarantee is typed tools and an exact,
  approved action, with its own least-privilege identities.
- **Name releases by digest.** Unreadable for people and for the model, and a digest says
  nothing about order; the tag is what the platform and the people working on it use.
- **Date every onset by pod events.** Less precise than a metric where one exists, and the
  simulator's and demo cluster's checks would change; the events are a fallback only.
- **Let RECAP's oracle stand in for the benchmark's.** Rejected: the benchmark's own grader is
  the independent judge, and RECAP's oracle sees less on a target without probes.
