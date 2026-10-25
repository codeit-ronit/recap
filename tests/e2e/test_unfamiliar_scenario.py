"""The held-out UNFAMILIAR scenario end to end (scripted policy, simulated environment).

m3-inventory-leak-behind-frontend: the alert names frontend, two calls away from the cause;
the services log in an RPC client's style; frontend's own release is hours old. RECAP must
follow the failed calls down to inventory, reject the old release, and restart inventory.
"""

from __future__ import annotations

import pytest

from recap.core.config import Settings
from recap.core.models import FailureMechanism, HypothesisStatus, IncidentState, RunConfig
from recap.runtime import Runtime
from tests.llm_fakes import OperatorApproves

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("strategy", ["adaptive", "parallel", "single"])
async def test_the_unfamiliar_incident_is_traced_to_inventory(
    settings: Settings, strategy: str
) -> None:
    runtime = Runtime(settings)
    record = await runtime.run_incident(
        "m3-inventory-leak-behind-frontend",
        RunConfig(strategy=strategy, use_memory=False),
        approval_channel=OperatorApproves(runtime.approvals),
    )
    assert record.state == IncidentState.RESOLVED
    assert [(a.action, a.target) for a in record.actions] == [("restart_service", "inventory")]
    report = record.verification
    assert report is not None and report.best is not None
    assert (report.best.mechanism.value, report.best.target_service) == (
        "resource_pressure",
        "inventory",
    )
    # The RPC-style lines are what the chain was read from: supported failed calls lead
    # from frontend through cart or checkout down to inventory, and the lines each of those
    # checks relied on are written only in the RPC client's phrasing. (Inventory's OOM kills
    # alone would also find inventory under adaptive and parallel routing, so the outcome
    # above does not show this.)
    calls = {
        (c.hypothesis.target_service, c.hypothesis.details.get("dependency")): c
        for c in report.checks
        if c.hypothesis.mechanism == FailureMechanism.DEPENDENCY_FAILURE
        and c.hypothesis.status == HypothesisStatus.SUPPORTED
    }
    middle = [s for s in ("cart", "checkout") if {("frontend", s), (s, "inventory")} <= set(calls)]
    assert middle, f"no supported chain from frontend to inventory: {sorted(calls)}"
    evidence = {item.id: item for item in runtime.runs.evidence(record.run_id)}
    for caller, dependency in [("frontend", middle[0]), (middle[0], "inventory")]:
        check = calls[(caller, dependency)]
        assert "explained by resource pressure on inventory" in check.passed
        relied_on = [evidence[eid] for eid in check.basis_evidence_ids]
        assert relied_on and {item.tool_name for item in relied_on} == {"get_service_logs"}
        errors = [
            line["message"]
            for item in relied_on
            for line in item.data["lines"]
            if line["level"] == "ERROR"
        ]
        assert any(f"rpc to {dependency} failed" in message for message in errors)
        assert not any(f"upstream {dependency} call failed" in message for message in errors)
    assert record.episode_id is None  # a held-out scenario never becomes memory
