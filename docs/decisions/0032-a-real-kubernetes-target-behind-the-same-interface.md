# ADR 0032: A real Kubernetes target behind the same interface: typed kubectl, one identity per role, a pinned cluster, and a person who types the service's name

**In one sentence:** RECAP works on a real Kubernetes cluster through one more
`TargetEnvironment` adapter, chosen by a target profile. The adapter calls `kubectl` only
through typed operations, with one least-privilege identity per role, only after proving the
cluster is the pinned one. It writes only after a person typed the target service's name for
that exact action. An oracle with its own identity decides whether the system recovered.

## Status

Accepted (M5, W1 to W6; built 2026-11-02 to 2026-11-07, recorded at the release candidate).
Builds on [ADR 0007](0007-replaceable-environment-adapter.md) (one adapter per target),
[ADR 0014](0014-signed-capabilities-at-the-write-boundary.md) (the adapter verifies every
write's capability) and [ADR 0015](0015-operator-auth-and-no-wins-approvals.md) (a person's
exact approval, "no" wins). [ADR 0031](0031-an-external-benchmark-harness-release-tags-and-pod-failure-onsets.md)
extends it to an external benchmark's cluster. The design was planned in
[m5-integration-plan.md](../m5-integration-plan.md), and what happened is in
[m5-progress.md](../m5-progress.md).

## Context

Until M4, RECAP fixed only its own simulated shop. A real cluster changes what can go wrong:
- a model that could run arbitrary commands could do arbitrary damage;
- a stale or borrowed credential could reach the wrong cluster;
- an approval given without attention could change a real system;
- a write that "succeeded" proves nothing about recovery.

The rest of RECAP (agents, gateway, verifier, memory, dashboard) had to stay the same, so the
results would mean the same thing on both targets.

## Decision

1. **The same interface, a target profile.** `KubernetesEnvironment`
   ([recap/environments/kubernetes/](../../recap/environments/kubernetes/)) implements
   `TargetEnvironment`. `RECAP_TARGET=kind-demo` or `sregym-hotel` loads a profile
   (`configs/targets/<name>/`: environment, policies, budget, knowledge overlay). Without it
   nothing changes: the simulator, its tests and CI stay as they were.
2. **Typed operations, never a command string.** Each `kubectl` call is a typed operation built
   from validated names. No shell is involved, and no model text ever becomes an argument.
   Each identity may run only the operations its role needs.
3. **One identity per role, least privilege.** `recap-reader` serves the read tools;
   `recap-oracle` reads for the oracle; `recap-writer` may only patch the app's named
   Deployments (and list their ReplicaSets); `recap-scenario` injects the Kind demo's faults.
   Tokens are short-lived, minted on the cluster's host, and kept outside git with mode 600.
   The administrator's credential is refused. On the Kind demo, an admission policy also
   checks the shape of every patch the writer or scenario sends.
4. **A pinned cluster, one run at a time.** Before anything runs, each kubeconfig's API server
   must be on 127.0.0.1 and its CA fingerprint must match the one recorded at setup. The
   versions must be within one minor of each other, and the monitoring clock (Prometheus, where
   there is one) must agree. A namespace lease keeps two runs apart, and a write switch in the
   state folder stops every write while reads go on.
5. **The single write path.** Only rollback and restart. The capability is verified and its
   nonce committed before the cluster is touched, the target is checked again, then a
   server-side dry run, the write, a read-back and an audit row follow, all within the 10 s
   tool limit. A rollback restores exactly the image the named revision ran.
6. **A person approves every write on a live target by typing the service's name.** At the
   console an Enter alone asks again, and the prompt ends at the request's expiry. Through the
   dashboard or the API, an approval without the exact name decides nothing. The decision log
   records how and when. The simulation auto-approver, replays and hosted models are refused
   on a live target.
7. **An independent oracle.** It uses its own identity and is never an MCP tool. It checks
   that the rollout is complete and the replicas ready, and it runs its own HTTP probe (two
   rounds). Where a target has no probe, it waits for containers that stayed up a minute with
   no exit in five. The controller keeps probing until the system is healthy or its
   stabilization window ends.
8. **Where it runs.** The clusters and the model run on the owner's AWS VM; RECAP stays on the
   Mac and reaches them through an SSH tunnel ([infra/aws/ec2.sh](../../infra/aws/ec2.sh)). A
   guard on the VM stops it when nothing uses it, and `ec2.sh hold` keeps it up for one run
   (2 hours at most).

## Consequences

- The agents, tools, gateway, verifier and dashboard are the same on both targets. Only the
  adapter, the target profile and the runbook overlay are new.
- A run on a real cluster cannot be replayed (a replay is refused against a live target). Its
  recording is evidence, and its evaluation record has a mode of its own, `real_kubernetes`.
- Every live fix is human-in-the-loop. The approval proves a terminal or a token holder and a
  timing, not which person decided; per-person identity is future work.
- Running the clusters and a CPU model on one rented VM is slow (minutes per model request)
  and costs money while it runs, so the VM is stopped whenever it is idle.

## Alternatives considered

- **Give the model a kubectl or shell tool.** Rejected: free-form writes cannot be checked
  against an exact approved action. This is also why RECAP does not use SREGym's own agent
  tools.
- **Use the cluster administrator's credential.** Rejected: one leaked token would control the
  whole cluster.
- **Run the cluster and the model on the Mac.** Tried first (W2 and W3 ran on a colima VM).
  With the model beside the cluster, the Mac ran out of memory, so both moved to the VM.
- **A managed cloud cluster.** Not needed for one small app, and more costly to keep running.
- **One-click approval in the dashboard.** Rejected in W6: on a live target the dashboard asks
  for the service's name, as the console does.
