"""The run store lists a parallel round's evidence in one order, whichever agent was slower.

The store is what the API and the exports serve (GET /runs/{id}/evidence), so its order must
be the canonical one the agents and the verifier used: by agent, then each agent's own order.
Each live run here forces a parallel round against a fake Ollama server and delays one
specialist's answers (tests/e2e/test_parallel_replay.py).
"""

from __future__ import annotations

import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState
from tests.e2e.test_parallel_replay import live_run

pytestmark = pytest.mark.e2e


async def test_stored_evidence_order_does_not_depend_on_which_agent_was_slower(
    settings: Settings,
) -> None:
    orders = []
    for slow_role in ("telemetry_investigator", "infrastructure_investigator"):
        runtime, record = await live_run(settings, slow_role)
        assert record.state == IncidentState.RESOLVED
        orders.append([e.id.split("-")[-1] for e in runtime.runs.evidence(record.run_id)])
    assert orders[0] == orders[1]
    assert any(suffix.startswith("r1") for suffix in orders[0])  # a parallel round ran
