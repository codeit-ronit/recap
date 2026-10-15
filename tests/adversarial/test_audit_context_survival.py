"""Audit finding 2.1 (context loss): an LLM never sees the newest lines of a long tool result.

What the audit found
--------------------
A tool result reaches an LLM provider in two steps, and each step can shorten it:

1. The tool gateway (recap/tools/gateway.py, `_record` and `_render`) stores the tool's
   output as compact JSON text in `Evidence.content`. Text longer than 6000 characters
   (`limits.max_output_chars` in configs/policies.yaml) is cut, and the cut keeps the
   FIRST 6000 characters.
2. The context packer (recap/context/packer.py, `_compact`) shortens every item above
   `max_item_tokens` (400 estimated tokens, about 1600 characters, configs/budget.yaml)
   to about its FIRST 1600 characters plus a pointer to the full record.

Logs and metric series are chronological: oldest first, newest last. So both cuts throw
away the newest lines, and those are the lines that explain an incident. On the real
simulator, the inventory logs of test-inventory-bad-release have 39 lines and 18 of them
are ERROR lines. After packing, not one ERROR line is left in the text.

An LLM provider only reads `ContextPack.items[].text` (recap/providers/prompts.py,
`_context_section`). The scripted provider reads the full structured `Evidence.data`
instead (recap/harness/loop.py), which is why V1 never noticed.

How these tests reproduce it
----------------------------
Every test checks only what an LLM would be shown: the text of the packed items.

* Synthetic log and metric results go through the real ToolGateway read path. Only the
  MCP client is replaced, by a tiny transport that returns a canned observation, so the
  evidence text is rendered exactly as in production.
* Real simulator logs (the two scenarios the audit measured) are recorded the same way.
  A wide query (up to 100 lines from the last 240 minutes) shows the gateway's cut on its
  own: even in a pack with room for everything, payment's logs show none of their 3 ERROR
  lines, and inventory's logs lose their 10 newest ERROR lines.
* One end-to-end test runs the real controller (real MCP subprocess, simulator and
  gateway) with the scripted provider, and keeps a copy of every context pack that the
  provider was handed.
* One guard test (it passes today) keeps a fix honest: evidence that the controller marks
  as essential (`priority_ids`, for example evidence that contradicts a hypothesis) must
  still win a tight pack budget.

Status: these tests were strict xfails in M0 (they failed on the V1 code). M1 fixed the
packer (structured summaries from recap/context/summaries.py; compaction keeps the head and
the tail) and the gateway (its text cap keeps the head and the tail), so they now pass and
stay as regression tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from recap.context.evidence import EvidenceRegistry
from recap.context.packer import ContextPacker
from recap.core.config import Settings
from recap.core.models import (
    AgentRole,
    ContextPack,
    Contract,
    DiscoveredTool,
    EnvironmentInfo,
    Evidence,
    Incident,
    InvestigationDecision,
    InvestigationRequest,
    ProposalDecision,
    ProposalRequest,
    RawToolResult,
    RunBudget,
    RunConfig,
)
from recap.core.observations import (
    AlertSignal,
    DeploymentList,
    DeploymentRecord,
    LogLine,
    LogQueryResult,
    MetricPoint,
    MetricSeries,
)
from recap.environments.simulated import SimulatedEnvironment
from recap.environments.simulated.scenario import load_scenario
from recap.harness.budget import BudgetTracker
from recap.providers.deterministic import DeterministicProvider
from recap.runtime import Runtime
from recap.safety.approvals import ApprovalStore
from recap.safety.policy import PolicyEngine
from recap.telemetry.redact import Redactor
from recap.telemetry.tracer import MemorySink, Tracer
from recap.tools.gateway import RemediationLocks, ToolGateway
from recap.tools.registry import ToolRegistry

T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)  # alert time of the synthetic checkout incident
SHOP = EnvironmentInfo(kind="simulated", name="test simulator", namespace="shop", is_simulated=True)
CHECKOUT_INCIDENT = Incident(
    incident_id="inc-ctx-1",
    scenario_id="synthetic",
    service="checkout",
    affected_services=["checkout"],
    symptoms=["checkout: HTTP 5xx ratio above 30%"],
    created_at=T0,
    environment="simulated",
    budget=RunBudget(),
)
NEWEST_ERROR = "NEWEST-ERROR-MARKER-7f3a"  # written only into the newest line of a synthetic log
PEAK_MS = 3170.0  # the highest point of the synthetic latency spike
LATEST_MS = 2905.0  # the newest point: still far above the ~80 ms baseline
ROOMY = 100_000  # an item cap and pack size so large that nothing is compacted or dropped
# The two scenarios the audit measured, each with the service whose logs show the fault.
AUDITED = [("test-inventory-bad-release", "inventory"), ("test-payment-leak-cascade", "payment")]


# --- helpers: record evidence the production way ------------------------------------------


class CannedTransport:
    """Stands in for the MCP client: every call to a tool returns that tool's canned output.

    An output is the observation model dumped with mode="json". That is exactly the
    structured content the MCP server sends back for the same observation.
    """

    transport_name = "canned-transport (test)"

    def __init__(self) -> None:
        self.outputs: dict[str, dict[str, Any]] = {}  # tool name -> structured output

    async def list_tools(self) -> list[DiscoveredTool]:
        return []  # tool discovery is not part of these tests

    async def call_tool(
        self, name: str, args: dict[str, object], timeout_seconds: float
    ) -> RawToolResult:
        return RawToolResult(ok=True, structured=self.outputs[name])


class Recorder:
    """The real ToolGateway read path, with the canned transport and one evidence registry."""

    def __init__(
        self, settings: Settings, incident: Incident, environment: EnvironmentInfo = SHOP
    ) -> None:
        policies = settings.policies
        registry = ToolRegistry(policies)
        self.transport = CannedTransport()
        self.evidence = EvidenceRegistry("run-ctx0000001", incident.incident_id)
        self.gateway = ToolGateway(
            transport=self.transport,
            registry=registry,
            policy=PolicyEngine(policies, registry),
            approvals=ApprovalStore(),
            budget=BudgetTracker(RunBudget()),
            evidence=self.evidence,
            tracer=Tracer("run-ctx0000001", [MemorySink()]),
            incident=incident,
            environment=environment,
            limits=policies.limits,  # production bounds: 6000 characters, 60 list items
            target_scope="sandbox:run-ctx0000001",
            locks=RemediationLocks(),  # fresh locks, so tests never share state
            redact=Redactor(environ={}),  # production redaction, minus this machine's env
        )

    async def record(self, tool: str, args: dict[str, Any], output: Contract) -> Evidence:
        """Call `tool` through the gateway. The transport answers with `output`."""
        self.transport.outputs[tool] = output.model_dump(mode="json")
        result = await self.gateway.call_read(tool, args, AgentRole.INVESTIGATOR)
        assert result.ok and result.evidence_id, result.error  # the call itself must work
        evidence = self.evidence.get(result.evidence_id)
        assert evidence is not None
        return evidence


def pack(
    settings: Settings,
    incident: Incident,
    evidence: Sequence[Evidence],
    *,
    max_item_tokens: int | None = None,
    max_tokens: int | None = None,
    priority_ids: Sequence[str] = (),
) -> ContextPack:
    """Pack evidence like every agent step does (recap/harness/loop.py).

    Without overrides it uses the configured sizes from configs/budget.yaml:
    max_item_tokens 400 and pack_tokens 3000.
    """
    context = settings.budget.context
    item_cap = context.max_item_tokens if max_item_tokens is None else max_item_tokens
    pack_size = context.pack_tokens if max_tokens is None else max_tokens
    return ContextPacker(max_item_tokens=item_cap).build(
        incident=incident, evidence=evidence, max_tokens=pack_size, priority_ids=priority_ids
    )


def llm_text(pack: ContextPack) -> str:
    """All that an LLM provider is shown of a pack: the text of every packed item.

    recap/providers/prompts.py (_context_section) puts exactly item.text into the prompt.
    """
    return "\n".join(item.text for item in pack.items)


# --- helpers: log lines -------------------------------------------------------------------


def shows(text: str, line: LogLine) -> bool:
    """True if `text` shows this log line: its message and the minute it was logged.

    Only the message and the HH:MM time are checked, so a fix may lay lines out in any
    way (JSON, plain text, a summary of repeated errors) and still pass.
    """
    return line.message in text and line.timestamp.strftime("%H:%M") in text


def newest_error(lines: Sequence[LogLine]) -> LogLine:
    """The newest ERROR line. Log results are chronological: oldest first, newest last."""
    return [line for line in lines if line.level == "ERROR"][-1]


def error_report(text: str, lines: Sequence[LogLine]) -> str:
    """Failure message: how many ERROR lines the LLM can see, and the newest one it misses."""
    errors = [line for line in lines if line.level == "ERROR"]
    seen = sum(shows(text, line) for line in errors)
    newest = errors[-1]
    return (
        f"{seen} of {len(errors)} ERROR lines are visible to the LLM; "
        f"the newest is missing: {newest.timestamp:%H:%M} {newest.message}"
    )


# --- helpers: observations ----------------------------------------------------------------


def checkout_log(info_lines: int, error_lines: int) -> LogQueryResult:
    """Routine INFO lines, then ERROR lines; the newest ERROR line carries NEWEST_ERROR.

    One line per minute, ending at T0: the errors are the newest lines, as when a service
    breaks shortly before the alert fires.
    """
    total = info_lines + error_lines
    lines: list[LogLine] = []
    for i in range(total):
        at = T0 - timedelta(minutes=total - 1 - i)
        if i < info_lines:
            level, message = "INFO", f"GET /api/checkout/orders 200 in {80 + i % 20}ms"
        else:
            level, message = "ERROR", "PricingClient call failed: timeout after 3000ms"
        lines.append(LogLine(timestamp=at, level=level, service="checkout", message=message))
    newest = lines[-1]
    lines[-1] = newest.model_copy(update={"message": f"{newest.message} {NEWEST_ERROR}"})
    return LogQueryResult(service="checkout", lines=lines, total_matched=total)


def latency_spike(window_minutes: int) -> MetricSeries:
    """p95 latency every 5 minutes: about 80 ms, then a spike in the last three points."""
    values = [80.0 + k % 5 for k in range(window_minutes // 5 + 1)]  # 49 points for 240 min
    values[-3:] = [1250.0, PEAK_MS, LATEST_MS]  # spike starts, peaks, is still far too high
    newest = len(values) - 1
    points = [
        MetricPoint(timestamp=T0 - timedelta(minutes=5 * (newest - k)), value=value)
        for k, value in enumerate(values)
    ]
    return MetricSeries(service="checkout", metric="latency_p95_ms", unit="ms", points=points)


def quiet_deployments() -> DeploymentList:
    """Checkout's release history: nothing new for two days, so no fresh bad release."""
    release = DeploymentRecord(
        service="checkout",
        version="v2.4.0",
        previous_version="v2.3.9",
        deployed_at=T0 - timedelta(days=2),
        change_summary="Price rounding fix",
    )
    return DeploymentList(deployments=[release])


async def simulator(
    settings: Settings, tmp_path: Path, scenario_id: str
) -> tuple[SimulatedEnvironment, Incident]:
    """A fresh simulated shop reset into `scenario_id`, plus the incident its alert opens."""
    scenarios = Path(settings.environment.options["scenarios_dir"])
    environment = SimulatedEnvironment(
        run_id="run-ctx", scenarios_dir=str(scenarios), state_dir=str(tmp_path / "sandbox")
    )
    alert = await environment.reset(scenario_id, load_scenario(scenarios, scenario_id).seed)
    return environment, incident_for(scenario_id, alert)


def incident_for(scenario_id: str, alert: AlertSignal) -> Incident:
    """The incident the controller would open for this alert."""
    return Incident(
        incident_id="inc-ctx-2",
        scenario_id=scenario_id,
        service=alert.service,
        affected_services=alert.affected_services,
        symptoms=alert.symptoms,
        created_at=alert.fired_at,
        environment="simulated",
        budget=RunBudget(),
    )


# --- 1. a long log whose errors are at the end --------------------------------------------


async def test_an_error_at_the_end_of_a_long_log_reaches_the_llm(settings: Settings) -> None:
    """60 INFO lines, then 5 ERROR lines: the newest ERROR (the marker) is visible after packing."""
    recorder = Recorder(settings, CHECKOUT_INCIDENT)
    # The gateway keeps the newest 60 of the 65 lines (correct), but their JSON is longer
    # than 6000 characters, and its text cap keeps the beginning. Then the packer keeps
    # only the first 1600 characters of what is left.
    evidence = await recorder.record(
        "get_service_logs",
        {"service": "checkout", "limit": 100},
        checkout_log(info_lines=60, error_lines=5),
    )

    packed = pack(settings, CHECKOUT_INCIDENT, [evidence])

    assert NEWEST_ERROR in llm_text(packed), "the newest ERROR line is not visible to the LLM"


# --- 2. real simulator logs ---------------------------------------------------------------


@pytest.mark.parametrize(("scenario_id", "service"), AUDITED)
async def test_the_newest_error_in_real_simulator_logs_reaches_the_llm(
    settings: Settings, tmp_path: Path, scenario_id: str, service: str
) -> None:
    """The failing service's newest ERROR line is still visible after packing."""
    environment, incident = await simulator(settings, tmp_path, scenario_id)
    # The MCP tool's defaults (limit=40, since_minutes=60): what an agent gets by default.
    # Under 6000 characters, so the gateway keeps every line and only the packer cuts.
    logs = await environment.observe_logs(service, limit=40, since_minutes=60)
    recorder = Recorder(settings, incident, environment.describe())
    evidence = await recorder.record("get_service_logs", {"service": service}, logs)

    text = llm_text(pack(settings, incident, [evidence]))

    assert shows(text, newest_error(logs.lines)), error_report(text, logs.lines)


@pytest.mark.parametrize(("scenario_id", "service"), AUDITED)
async def test_a_wide_log_query_keeps_its_newest_error_even_with_room_to_spare(
    settings: Settings, tmp_path: Path, scenario_id: str, service: str
) -> None:
    """Asking for more log lines must not hide the newest ERROR line from the LLM."""
    environment, incident = await simulator(settings, tmp_path, scenario_id)
    # The widest query the policy allows: up to 100 lines from the last 240 minutes.
    logs = await environment.observe_logs(service, limit=100, since_minutes=240)
    recorder = Recorder(settings, incident, environment.describe())
    args = {"service": service, "limit": 100, "since_minutes": 240}
    evidence = await recorder.record("get_service_logs", args, logs)

    # A pack with room for everything: nothing is compacted or dropped, so whatever is
    # missing below was already cut by the gateway before the packer saw it.
    roomy = pack(settings, incident, [evidence], max_item_tokens=ROOMY, max_tokens=ROOMY)
    text = llm_text(roomy)

    # The gateway rightly keeps the newest 60 lines, so the newest ERROR line is among them.
    assert shows(text, newest_error(logs.lines)), error_report(text, logs.lines)


# --- 3. a long metric series whose spike is at the end ------------------------------------


async def test_the_peak_and_latest_value_of_a_long_metric_series_reach_the_llm(
    settings: Settings,
) -> None:
    """A 240-minute latency series that spikes at the end: its peak and newest value are visible."""
    recorder = Recorder(settings, CHECKOUT_INCIDENT)
    args = {"service": "checkout", "metric": "latency_p95_ms", "window_minutes": 240}
    evidence = await recorder.record("get_service_metrics", args, latency_spike(240))

    text = llm_text(pack(settings, CHECKOUT_INCIDENT, [evidence]))

    # ":g" prints 3170.0 as "3170", which any layout of the number contains.
    assert f"{PEAK_MS:g}" in text, "the peak of the latency spike is not visible to the LLM"
    assert f"{LATEST_MS:g}" in text, "the newest latency value is not visible to the LLM"


# --- 4. end to end: what a provider is handed in a real run -------------------------------


class PackRecordingProvider(DeterministicProvider):
    """The scripted provider, plus a copy of every context pack it was handed.

    The scripted policy decides from the full structured Evidence.data, so the run works
    as usual. The packs it keeps are exactly what an LLM provider would read instead.
    """

    def __init__(self) -> None:
        super().__init__()
        self.shown: list[tuple[ContextPack, dict[str, Evidence]]] = []  # (pack, its evidence)

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision:
        self.shown.append((request.context, request.evidence))
        return await super().next_action(request)

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        self.shown.append((request.context, request.evidence))
        return await super().propose_mitigation(request)


async def test_a_real_run_shows_its_provider_the_newest_inventory_error(
    settings: Settings,
) -> None:
    """End to end: every pack that holds inventory's logs also shows their newest ERROR line."""
    runtime = Runtime(settings)
    provider = PackRecordingProvider()
    runtime.make_provider = lambda config: provider  # type: ignore[method-assign]

    await runtime.run_incident(
        "test-inventory-bad-release", RunConfig(approval_mode="auto", memory_write=False)
    )

    # Every packed copy of inventory's logs, next to the full evidence it was cut from.
    shown = [
        (item.text, evidence[item.item_id])
        for context, evidence in provider.shown
        for item in context.items
        if item.source_ref.startswith("mcp:get_service_logs(")
        and "service=inventory" in item.source_ref
    ]
    assert shown, "the scripted policy always reads the alerted service's logs"
    for text, full in shown:
        lines = [LogLine.model_validate(line) for line in full.data["lines"]]
        assert shows(text, newest_error(lines)), error_report(text, lines)


# --- 5. guard: essential evidence still wins a tight budget -------------------------------


async def test_priority_evidence_still_wins_a_tight_pack_budget(settings: Settings) -> None:
    """Evidence passed as priority_ids is packed even when long ERROR logs compete for room."""
    recorder = Recorder(settings, CHECKOUT_INCIDENT)
    # Evidence that contradicts "bad deployment of checkout": no checkout release for days.
    contradicting = await recorder.record(
        "get_recent_deployments", {"service": "checkout"}, quiet_deployments()
    )
    # Three long, ERROR-heavy checkout logs. Each is compacted to the 400-token item cap.
    logs = [
        await recorder.record(
            "get_service_logs",
            {"service": "checkout", "since_minutes": minutes},
            checkout_log(info_lines=20, error_lines=15),
        )
        for minutes in (30, 60, 120)
    ]
    evidence = [contradicting, *logs]

    # Packed size of each item (compaction is per item, so the budget does not change it).
    everything = pack(settings, CHECKOUT_INCIDENT, evidence, max_tokens=ROOMY)
    sizes = {item.item_id: item.token_estimate for item in everything.items}
    # Room for one log or for the contradicting evidence, never for both.
    budget = sizes[contradicting.id] + min(sizes[log.id] for log in logs) - 1

    # Without priority a log takes the room, so this budget really forces a choice.
    plain = pack(settings, CHECKOUT_INCIDENT, evidence, max_tokens=budget)
    assert contradicting.id in plain.dropped_ids

    packed = pack(
        settings, CHECKOUT_INCIDENT, evidence, max_tokens=budget, priority_ids=[contradicting.id]
    )

    assert contradicting.id in packed.ids()
    assert "v2.4.0" in llm_text(packed)  # its content is visible, not just its ID
    assert packed.tokens_after <= budget
    assert sorted(packed.dropped_ids) == sorted(log.id for log in logs)  # listed, never lost
