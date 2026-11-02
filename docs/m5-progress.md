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

**Next**
- The Kubernetes environment adapter (offline-tested with a fake client) and the Kind demo
  app, manifests and scripts are being built; both need no cluster.
