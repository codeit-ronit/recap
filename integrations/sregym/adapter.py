"""SREGym adapter (M5 W5): RECAP on one problem of SREGym's, on SREGym's own Kind cluster.

SREGym (release 1.1, commit a90b43cc973158120119a9f388081eac4f7adb36) deploys one of its
applications and injects one problem in its external-harness mode, then leaves the cluster
to the agent; its own executable mitigation oracle grades the cluster state afterwards
(infra/sregym/). This adapter is RECAP's Kubernetes adapter (recap/environments/kubernetes/) in
the mode such a target needs (ADR 0031):

* the harness owns the fault: reset changes nothing, takes the app's first revisions as the
  run's baseline and waits for the alert from the cluster's own state;
* a release is an image tag (SREGym tags by date or commit and pins by digest), and only a key
  into a Deployment's own revision history;
* there is no Prometheus and no probe: health is status and readiness, said as such.

RECAP reads and writes through its own least-privilege ServiceAccounts in SREGym's cluster
(infra/sregym/grant-recap.sh), never through SREGym's own agent tools (a kubectl passthrough
and a shell, both cluster-wide) and never with SREGym's administrator credential. Selected by
RECAP_TARGET=sregym-hotel (configs/targets/sregym-hotel/).
"""

from __future__ import annotations

from typing import Any

from recap.core.errors import ConfigError
from recap.core.models import EnvironmentInfo
from recap.environments.kubernetes import KubernetesEnvironment


class SREGymEnvironment(KubernetesEnvironment):
    """The Kubernetes adapter for a SREGym problem (external harness, image-tag releases)."""

    kind = "sregym"

    def __init__(self, run_id: str, **options: Any) -> None:
        options = {"harness": "external", "release_format": "tag", **options}
        if options["harness"] != "external":
            raise ConfigError(
                "the SREGym adapter runs only in external-harness mode: SREGym injects the fault"
            )
        super().__init__(run_id, **options)

    def describe(self) -> EnvironmentInfo:
        """Static and offline: what this target is. No path, URL, secret or problem name."""
        options = self.options
        what = (
            f"SREGym 1.1 on its own Kind cluster {options.cluster} (context {options.context}), "
            f"not production: {len(self.services)} Deployments of a benchmark application in "
            f"namespace {self.namespace}, deployed and broken by SREGym's own harness, read "
            "through typed kubectl operations with RECAP's own identities. The only writes are a "
            "rollback to a named revision and a rolling restart, each with a signed single-use "
            "capability; SREGym's own oracle grades the result."
        )
        if options.client == "fake":
            what = "OFFLINE FAKE of a SREGym target, for tests: synthetic data, no cluster. " + what
        info = super().describe()
        return info.model_copy(
            update={
                "name": f"SREGym on Kind cluster {options.cluster}"
                + (" (offline fake)" if options.client == "fake" else ""),
                "description": what,
            }
        )
