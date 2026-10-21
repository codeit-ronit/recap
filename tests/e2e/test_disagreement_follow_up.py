"""Two specialists with conflicting supported claims: RECAP asks once more, then hands over.

The fake model does not reason: it gives fixed answers per role. Telemetry claims memory
pressure on inventory, infrastructure claims a bad checkout release. In this two-fault
scenario both are true, both are supported and neither service's errors lead to the
other, so no evidence rule can choose. RECAP must not pick one by vote or by how much each
claim cites. It records an open disagreement, runs one follow-up round with the verifier's
question in each specialist's prompt, and then hands the incident to a person (FAILED)
without proposing any fix.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState, RunConfig, RunRecord
from recap.providers.factory import create_provider
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from tests.llm_fakes import (
    FakeOllama,
    OperatorApproves,
    ScriptedModel,
    call,
    hypothesis,
    step,
    tool_answer,
)

pytestmark = pytest.mark.e2e

TWO_FAULTS = """\
id: two-faults
title: "Checkout errors and inventory restarts at the same time"
description: "Test only: a bad checkout release and an unrelated inventory memory leak."
family: bad_deployment
partition: demo
seed: 41
tags: [test]
incident_time: "2026-03-15T14:30:00Z"
namespace: shop
alert:
  service: checkout
  affected_services: [checkout, inventory]
  symptoms:
    - "checkout: HTTP 5xx ratio above 30%"
    - "inventory: pods failing readiness"
services:
  frontend:  {version: v1.8.2, replicas: 3, dependencies: [checkout, cart]}
  checkout:  {version: v2.4.1, replicas: 3, dependencies: [payment]}
  payment:   {version: v1.12.3, replicas: 2}
  inventory: {version: v3.0.1, replicas: 2}
  cart:      {version: v2.0.0, replicas: 2}
deployments:
  - {service: frontend, version: v1.8.2, minutes_ago: 4320, change: "Responsive product grid"}
  - {service: checkout, version: v2.4.0, minutes_ago: 2880, change: "Price rounding fix"}
  - {service: checkout, version: v2.4.1, minutes_ago: 12, change: "Discount stacking"}
  - {service: payment, version: v1.12.3, minutes_ago: 3600, change: "Card network retry budget"}
  - {service: inventory, version: v3.0.1, minutes_ago: 4800, change: "Stock cache warm-up"}
  - {service: cart, version: v2.0.0, minutes_ago: 2400, change: "Persist carts"}
faults:
  - {type: bad_release, service: checkout, version: v2.4.1, started_minutes_ago: 11,
     error_rate: 0.38, latency_ms: 900, exception: "NullPointerException in DiscountEngine"}
  - {type: memory_leak, service: inventory, started_minutes_ago: 50, oom_minutes_ago: 8,
     error_rate: 0.42}
expected: {root_cause_service: checkout, mechanism: bad_deployment, fix: execute_rollback,
           fix_params: {to_version: v2.4.0}}
budget: {}
run_overrides: {}
requires_memory_of: []
"""
QUESTION_HEADER = "Open questions from RECAP's verifier"
UNTRUSTED = re.compile(r"<untrusted_data[^>]*>.*?</untrusted_data>", re.DOTALL)


class TwoViews:
    """Fixed answers per role (no reasoning). In a follow-up round, finish at once."""

    PLANS = {
        "telemetry_investigator": [
            [
                ("get_service_metrics", {"service": "inventory", "metric": "memory_mb"}),
                ("get_service_logs", {"service": "inventory"}),
            ],
            [
                ("get_service_metrics", {"service": "checkout", "metric": "error_rate"}),
                ("get_service_logs", {"service": "checkout"}),
            ],
        ],
        "infrastructure_investigator": [
            [
                ("get_recent_deployments", {"service": "checkout"}),
                ("get_k8s_like_events", {"service": "inventory"}),
            ],
        ],
    }
    CLAIMS = {
        "telemetry_investigator": {
            "target": "inventory",
            "mechanism": "resource_pressure",
            "version": None,
            "claim": "inventory runs out of memory",
        },
        "infrastructure_investigator": {
            "target": "checkout",
            "mechanism": "bad_deployment",
            "version": "v2.4.1",
            "claim": "checkout v2.4.1 broke checkout",
        },
    }

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []  # (role, user message) of every step

    def __call__(self, body: dict[str, Any]) -> Any:
        text = body["messages"][1]["content"]
        match = re.search(r"Role: (\S+)", text)
        if match is None:  # a proposal request: this test must never get that far
            raise AssertionError("RECAP asked for a fix although the claims conflict")
        role = match.group(1)
        self.prompts.append((role, text))
        number = int(text.rsplit("This is step ", 1)[1].split(" ", 1)[0])
        follow_up = QUESTION_HEADER in text
        plan = self.PLANS[role]
        planned = [] if follow_up or number > len(plan) else plan[number - 1]
        offered = ScriptedModel._offered(body)
        calls = [(name, args) for name, args in planned if name in offered]
        claim = self.CLAIMS[role]
        cited = ScriptedModel._visible_ids(body)
        found = hypothesis(
            claim["target"],
            cited,
            mechanism=claim["mechanism"],
            version=claim["version"],
            claim=claim["claim"],
        )
        if body.get("tools"):
            if calls:
                return tool_answer(*calls)
            finish = {"kind": "hypothesis", "summary": "", "next_checks": [], "hypotheses": [found]}
            return tool_answer(("submit_findings", finish))
        if calls:
            return step("tool_request", tool_calls=[call(name, **args) for name, args in calls])
        return step("hypothesis", hypotheses=[found])


def two_fault_settings(settings: Settings, tmp_path: Path) -> Settings:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / "two-faults.yaml").write_text(TWO_FAULTS)
    options = dict(settings.environment.options, scenarios_dir=str(scenarios))
    environment = settings.environment.model_copy(update={"options": options})
    return settings.model_copy(update={"environment": environment})


async def run(settings: Settings, model: TwoViews) -> tuple[Runtime, RunRecord]:
    runtime = Runtime(settings)
    mock = httpx.MockTransport(FakeOllama(responder=model).handle)

    def make(config: RunConfig) -> Any:
        return create_provider("ollama", runtime.settings, mock_http=mock, ledger=runtime.ledger)

    runtime.make_provider = make  # type: ignore[method-assign]
    record = await runtime.run_incident(
        "two-faults",
        RunConfig(provider="ollama", use_memory=False),
        approval_channel=OperatorApproves(runtime.approvals),
    )
    return runtime, record


async def test_conflicting_supported_claims_get_one_follow_up_round_then_a_person(
    settings: Settings, tmp_path: Path
) -> None:
    model = TwoViews()
    runtime, record = await run(two_fault_settings(settings, tmp_path), model)

    # Both claims are supported and level: the report keeps the conflict open.
    report = record.verification
    assert report is not None and report.best is None and report.status == "insufficient"
    statuses = {c.hypothesis.target_service: c.hypothesis.status.value for c in report.checks}
    assert statuses == {"inventory": "supported", "checkout": "supported"}
    conflict = [d for d in report.disagreements if d.kind == "competing_root_causes"]
    assert len(conflict) == 1 and not conflict[0].resolved
    assert conflict[0].claims == [
        "resource pressure on inventory",
        "bad deployment on checkout (v2.4.1)",
    ]
    assert conflict[0].question in report.open_questions()

    # One follow-up round, whose prompts carry the verifier's question for both specialists.
    assert [s.round for s in record.strategies] == [1, 2]
    events = runtime.events.list_events(record.run_id)
    rounds = [e.payload for e in events if e.category == C.INVESTIGATION_ROUND]
    assert rounds[0]["open_questions"] == [] and rounds[1]["open_questions"] == [
        conflict[0].question
    ]
    starts = [e for e in events if e.category == C.AGENT_START and e.payload["round"] == 2]
    assert {e.agent for e in starts} == {"telemetry_investigator", "infrastructure_investigator"}
    assert all(e.payload["questions"] == [conflict[0].question] for e in starts)
    follow_ups = [text for _, text in model.prompts if QUESTION_HEADER in text]
    assert {role for role, text in model.prompts if QUESTION_HEADER in text} == set(model.CLAIMS)
    for text in follow_ups:  # trusted text: outside every untrusted-data block
        outside = UNTRUSTED.sub("", text)
        assert conflict[0].question in outside
        assert outside.index(QUESTION_HEADER) < outside.index("## Your previous calls")

    # Then a person decides: no fix is proposed, approved or executed.
    assert record.state == IncidentState.FAILED
    reason = record.history[-1].reason
    assert "supported root causes still conflict" in reason and "a human should take over" in reason
    assert record.actions == [] and record.approvals == []
    assert not [e for e in events if e.category == C.PROPOSAL]
    merges = [e.payload for e in events if e.category == C.VERIFY_MERGE]
    assert len(merges) == 2 and merges[-1]["open_questions"] == [conflict[0].question]
