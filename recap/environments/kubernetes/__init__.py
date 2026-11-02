"""The Kind adapter: RECAP on a real, local Kubernetes cluster (milestone M5, target 1).

    options.py    what configs/targets/kind-demo/environment.yaml may set (checked, no I/O)
    kubectl.py    the closed set of typed Kubernetes operations; kubectl without a shell
    fake.py       an offline fake cluster that speaks the same JSON (tests only)
    translate.py  pure translation of Kubernetes JSON into RECAP's observation models
    rollout.py    revisions, the rollout-complete rule, releases and rollback plans
    metrics.py    fixed PromQL templates against the demo's Prometheus
    probe.py      the oracle's own HTTP probe, the alert rule, a scenario's HTTP burst
    preflight.py  cluster pinning: context, loopback API server, CA fingerprint, token
    state.py      per-run file (key, nonces, audits), namespace lease, write switch
    scenario.py   the typed schema of fixtures/kind-scenarios/*.yaml
    engine.py     KubernetesEnvironment, the TargetEnvironment (kind "kind")

See recap/environments/README.md and docs/m5-integration-plan.md (sections 3 and 4).
"""

from __future__ import annotations

from recap.environments.kubernetes.engine import KubernetesEnvironment

__all__ = ["KubernetesEnvironment"]
