# M5 progress: RECAP on a real Kubernetes target

One short entry per workstream. Plan: [m5-integration-plan.md](m5-integration-plan.md).
Nothing below claims a live result unless it says where it ran.

## W1: contracts and portable foundation (started 2026-11-02)

**Implemented**
- Audit of the integration points (TargetEnvironment, MCP tools, gateway, capabilities,
  oracle, scenarios, signals, API and evaluation guards): no contract change is needed for
  rollback and restart on Kubernetes; a target profile (`RECAP_TARGET`) keeps the simulator,
  its tests and CI unchanged.
- SREGym pinned for the benchmark work: release `1.1`, commit
  `a90b43cc973158120119a9f388081eac4f7adb36` (101 problems); CI-green fallback with later
  grading fixes: `51964ce0a911be3e71d9e1e75551bb0089dc70c7`. Its agent contract is HTTP
  (`/status`, `/get_app`, `/submit`) or the external-harness mode with its own mitigation
  oracle, so RECAP keeps its typed, guardrailed tools.
- Safety: a hosted model may not see a live target's data. The controller refuses a paid
  provider always, and a free hosted plan unless the owner sets
  `RECAP_ALLOW_HOSTED_MODEL_ON_LIVE_TARGET=1`, before the environment is reset.
  Redaction and the secret scan now cover JSON Web Tokens (Kubernetes ServiceAccount
  tokens) and inline kubeconfig key material.

**Verified**
- `tests/e2e/test_live_target_provider_scope.py` (4), the new redaction test, the secret
  scan (tracked files and the whole history) and the published-artifact checks: 67 passed;
  mypy clean.

**Blocked (needs the owner's approval)**
- Local cluster: `kind` and `kubectl` are not installed; a dedicated colima profile is
  proposed. SREGym: not feasible on this Mac (it needs 8 vCPU, 16 GB RAM and 100 GB of
  disk); an AWS VM is proposed.

**Then** (commits `cd23dcf`, `fce55f5`, `0b01cfa`): the Kubernetes environment adapter behind
`TargetEnvironment` (typed kubectl operations, a fake cluster for offline tests, a Kind target
profile) and the Kind demo app, manifests and scripts. Offline: 383 Kind-related tests passed.

## W2: a real Kind cluster (2026-11-02)

**Approved by the owner:** `brew install kind kubernetes-cli` (kind 0.33.0, kubectl 1.37.2) and
a dedicated colima profile `recap-m5` (4 CPU, 4 GiB, 20 GiB); the owner's default profile is
untouched.

**Verified on the real cluster** (commit `bf8057f`): `setup.sh` created the cluster and
deployed frontend, backend, Prometheus and the traffic generator (frontend 20 of 20 requests
answered); the four least-privilege identities' tokens are accepted. bad-release: 10 of 40
frontend requests failed, Prometheus 5xx share 0.199; reset: 0 of 40. injected-logs: 9 of 40
failed, the planted line in the backend's logs; reset: 0 of 40. Node container 742 MiB of
3.8 GiB.

## W3: authorization, permissions and recovery on the real cluster (2026-11-02)

**Found by the first live acceptance run** (1 of 6 passed) and fixed (`512a07d` and the next
commit): kubectl 1.37's `rollout restart` has no `--dry-run` (a restart is now the patch that
command makes, which a server-side dry run accepts); the alert fired before Prometheus had
scraped the errors, so the verifier saw a healthy platform and rightly refused the diagnosis
(reset now waits until each failing service shows its errors in Prometheus or cannot be
observed).

**Verified on the real cluster:** all six live acceptance tests pass
(`tests/e2e/test_kind_live.py`, `RECAP_LIVE_KIND=1`, scripted policy): reads are real and change
nothing; a bad release is rolled back only after an exact approval and the oracle verifies the
recovery; a rejected proposal changes nothing (BLOCKED); a restart fixes a full memory only
when readiness says so; the scripted pipeline resolves the report burst; the write boundary
holds for a direct MCP caller.

**Next:** W4, a real `qwen3:8b` investigation on the cluster with the owner approving the fix.
