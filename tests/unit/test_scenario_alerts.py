"""Every scenario's alert text must agree with what the simulator actually reports.

An alert saying "p95 latency above 2s" while the dashboard shows 600 ms would be an
inconsistent (and misleading) demo. This test checks each numeric symptom at alert time.
"""

from __future__ import annotations

import re
from pathlib import Path

from recap.core.config import Settings
from recap.environments.simulated.engine import SimulatedEnvironment

ERROR_RATE = re.compile(r"^(\w+): (?:HTTP 5xx ratio|checkout page error rate) above (\d+)%")
LATENCY = re.compile(r"^(\w+): p95 latency above ([\d.]+)(s|ms)")
READINESS = re.compile(r"^(\w+): readiness probe failures on (\d+) of (\d+) pods")


async def test_numeric_symptoms_match_simulated_signals(settings: Settings, tmp_path: Path) -> None:
    env = SimulatedEnvironment(
        run_id="alerts",
        scenarios_dir=settings.environment.options["scenarios_dir"],
        state_dir=str(tmp_path),
    )
    checked = 0
    for scenario in env.list_scenarios():
        alert = await env.reset(scenario.id, scenario.default_seed or 0)
        for symptom in alert.symptoms:
            if match := ERROR_RATE.match(symptom):
                health = await env.observe_health(match.group(1))
                assert health.error_rate * 100 > float(match.group(2)), (scenario.id, symptom)
            elif match := LATENCY.match(symptom):
                health = await env.observe_health(match.group(1))
                threshold = float(match.group(2)) * (1000 if match.group(3) == "s" else 1)
                assert health.latency_p95_ms > threshold, (scenario.id, symptom)
            elif match := READINESS.match(symptom):
                health = await env.observe_health(match.group(1))
                assert health.replicas - health.ready_replicas == int(match.group(2)), scenario.id
                assert health.replicas == int(match.group(3)), scenario.id
            else:
                continue  # qualitative symptom (e.g. user reports): nothing to compare
            checked += 1
    assert checked >= 25
