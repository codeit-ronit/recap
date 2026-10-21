"""The held-out UNFAMILIAR scenario end to end (scripted policy, simulated environment).

m3-inventory-leak-behind-frontend: the alert names frontend, two calls away from the cause;
the services log in an RPC client's style; frontend's own release is hours old. RECAP must
follow the failed calls down to inventory, reject the old release, and restart inventory.
"""

from __future__ import annotations

import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState, RunConfig
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
    # The RPC-style lines are what the chain was read from.
    logs = [
        e.content for e in runtime.runs.evidence(record.run_id) if e.tool_name == "get_service_logs"
    ]
    assert any("rpc to inventory failed" in text for text in logs)
    assert record.episode_id is None  # a held-out scenario never becomes memory
