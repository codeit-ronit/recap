# SREGym adapter

**Status: implemented in M5 (W5); development runs only, no benchmark results yet.** RECAP has
run on one SREGym problem, `misconfig_app_hotel_res`, on SREGym's own cluster on the owner's VM,
with the owner approving the fix at the terminal. Those runs shaped the adapter, so that
problem is not held out ([docs/m5-progress.md](../../docs/m5-progress.md)). M6 W5 froze a
held-out evaluation on five problems RECAP was never run on (`sregym-w5-1` to `-5`, blind
scenario files), not run yet: [docs/w5-protocol.md](../../docs/w5-protocol.md).

## What it is

`adapter.py` holds `SREGymEnvironment`: RECAP's Kubernetes adapter
([recap/environments/kubernetes/](../../recap/environments/kubernetes/)) in the mode a
benchmark's own cluster needs ([ADR 0031](../../docs/decisions/0031-an-external-benchmark-harness-release-tags-and-pod-failure-onsets.md)):

- **SREGym's harness owns the fault.** SREGym (release 1.1, commit
  `a90b43cc973158120119a9f388081eac4f7adb36`) deploys the app and injects the problem in its
  external-harness mode. RECAP's reset changes nothing: it takes the app's first revisions as
  the baseline and waits for the alert from the cluster's own state.
- **A release is an image tag**, used only as a key into a Deployment's own revision history.
  An approved rollback restores exactly the image that revision ran, digest included.
- **No probe and no metrics.** Health is a complete rollout, readiness, and containers that
  stay up (a crash loop is Running and Ready for the seconds each start lasts). Whatever this
  target cannot measure is reported as not measured, never estimated.

Agents keep the same MCP tools, gateway, approval and capability as on every other target.
RECAP works through three ServiceAccounts of its own (reader, writer, oracle; see
[infra/sregym/recap-rbac.yaml](../../infra/sregym/recap-rbac.yaml)), never through SREGym's
agent tools (a kubectl passthrough and a shell, cluster-wide), and never with SREGym's
administrator credential.

## How to use it

`RECAP_TARGET=sregym-hotel` selects the profile in
[configs/targets/sregym-hotel/](../../configs/targets/sregym-hotel/) (the environment, the
allowlists, the budget and the Kubernetes runbooks). The scenario is
[fixtures/sregym-scenarios/sregym-misconfig-app-hotel-res.yaml](../../fixtures/sregym-scenarios/sregym-misconfig-app-hotel-res.yaml).
Setting up SREGym on the VM, granting RECAP's identities, the run and the grading are in
[infra/sregym/README.md](../../infra/sregym/README.md).

## Grading

Each run gets two verdicts, never merged. RECAP's oracle decides RECAP's RESOLVED. SREGym's
mitigation oracle (`run-oracle.py`, exit 0 means pass) is the benchmark's verdict. A person
approves every write, so these runs are human-in-the-loop and not comparable with SREGym's
leaderboard.

## Tests

- [tests/unit/test_sregym_adapter.py](../../tests/unit/test_sregym_adapter.py): the adapter's
  self-description, the profile and scenario as `RECAP_TARGET=sregym-hotel` loads them, and
  that nothing about the problem's answer reaches an agent.
- [tests/unit/test_kind_external.py](../../tests/unit/test_kind_external.py): external-harness
  mode over the offline fake cluster (synthetic seed in `tests/fixtures/sregym/`). It covers
  reset, reads without monitoring, a tagged rollback, and a crash loop that never reads as
  recovered.

## The rule

Never report SREGym benchmark results from these runs. The problem they used is a development
problem; a benchmark claim needs SREGym problems RECAP was never run on, a pre-committed split
([docs/benchmark-plan.md](../../docs/benchmark-plan.md)), and runs that are not tuned in
between.
