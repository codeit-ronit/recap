"""How the controller carries a round's outcome forward (M3 review fixes).

- A lead (a dependency failure nothing explains yet) reaches the next round's agents next to
  the verifier's open questions, but it is no disagreement: it never routes RT3-disagreement.
- A person is told what is really open: level SUPPORTED root causes, or claims naming
  different bad releases that the evidence settles none of.
- Findings kept from a round a budget stop cut short are labelled partial in the record.
- A replay reads memory as its recording found it, never the episode its original wrote.
"""

from __future__ import annotations

from typing import Any

import pytest

from recap.agents.verifier import EvidenceVerifier
from recap.core.config import Settings
from recap.core.models import (
    Disagreement,
    IncidentState,
    RunBudget,
    RunConfig,
    VerificationReport,
)
from recap.harness.controller import _handover
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from tests.e2e.test_parallel_replay import live_run, replay_run

pytestmark = pytest.mark.e2e

LEAD = (
    "checkout's calls to payment fail and nothing on payment itself is confirmed yet: find "
    "out what is wrong with payment (its errors, memory, restarts and releases)."
)


class FirstRoundLeads:
    """The real verifier, except that its first report is unsolved and carries one lead."""

    def __init__(self) -> None:
        self.inner = EvidenceVerifier()
        self.reports = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def verify(self, *args: Any, **kwargs: Any) -> VerificationReport:
        report = self.inner.verify(*args, **kwargs)
        self.reports += 1
        if self.reports == 1:
            unsolved = {"status": "insufficient", "best": None, "leads": [LEAD]}
            return report.model_copy(update=unsolved)
        return report


async def test_a_lead_reaches_the_next_round_but_is_no_disagreement(settings: Settings) -> None:
    runtime = Runtime(settings)
    runtime.verifier = FirstRoundLeads()  # type: ignore[assignment]
    config = RunConfig(strategy="single", use_memory=False, approval_mode="auto")
    record = await runtime.run_incident("bad-deployment", config)

    events = runtime.events.list_events(record.run_id)
    rounds = [e.payload for e in events if e.category == C.INVESTIGATION_ROUND]
    assert [r["leads"] for r in rounds] == [[], [LEAD]]
    assert [r["open_questions"] for r in rounds] == [[], []]
    second = [e.payload for e in events if e.category == C.ROUTING][1]
    assert second["signals"]["disagreement"] is False
    assert second["rule_id"] != "RT3-disagreement"
    starts = [e.payload for e in events if e.category == C.AGENT_START]
    assert LEAD in starts[-1]["questions"]  # the agent of round 2 was asked
    assert record.state == IncidentState.RESOLVED


def report_with(*disagreements: Disagreement) -> VerificationReport:
    return VerificationReport(status="insufficient", disagreements=list(disagreements))


def test_the_handover_names_what_is_really_open() -> None:
    release = Disagreement(
        kind="detail_conflict",
        claims=["bad deployment on checkout (v2.4.0)", "bad deployment on checkout (v2.4.1)"],
        resolved=False,
        question="Claims disagree on which release of checkout is bad (v2.4.0 and v2.4.1).",
    )
    level = Disagreement(
        kind="competing_root_causes",
        claims=["bad deployment on payment (v1.13.0)", "resource pressure on inventory"],
        resolved=False,
        question="Two root causes are supported and equally deep.",
    )
    settled = level.model_copy(update={"resolved": True, "question": ""})

    assert _handover(report_with()) == ""
    assert _handover(report_with(settled)) == ""
    only_release = _handover(report_with(release))
    assert only_release.startswith("claims still disagree on which release is bad")
    assert "supported" not in only_release and "v2.4.0" in only_release
    # The real conflict of supported causes is named, even when a release question came first.
    both = _handover(report_with(release, level))
    assert both.startswith("supported root causes still conflict")
    assert "resource pressure on inventory" in both and "checkout" not in both


async def test_findings_kept_from_a_stopped_round_are_labelled_partial(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    config = RunConfig(
        strategy="parallel",
        use_memory=False,
        approval_mode="auto",
        budget=RunBudget(max_tool_calls=3),
    )
    record = await runtime.run_incident("bad-deployment", config)

    assert record.state == IncidentState.BUDGET_EXHAUSTED
    kept = [
        e.payload
        for e in runtime.events.list_events(record.run_id)
        if e.category == C.ORCHESTRATION and "partial_findings_kept" in e.payload
    ]
    assert kept, "the shared budget stopped the round with another agent's finding complete"
    roles = set(kept[0]["partial_findings_kept"])
    partial = [f for f in record.findings if f.partial]
    assert {f.agent_role.value for f in partial} == roles
    assert all(not f.partial for f in record.findings if f.agent_role.value not in roles)


async def test_a_replay_reads_memory_as_its_recording_found_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RunConfig(provider="ollama", use_memory=True, strategy="single")
    _, live = await live_run(settings, None, config)
    assert live.state == IncidentState.RESOLVED and live.memory_hits == []
    assert live.episode_id  # it wrote a LIVE episode when it ended

    # Same data folder: before the fix the replay found that episode (similarity 1.0), so
    # its prompts differed from the recording and it ended with ReplayMismatch.
    runtime, replay = await replay_run(settings, monkeypatch, live, config)
    assert replay.state == IncidentState.RESOLVED, replay.error
    assert replay.memory_hits == []
    [search] = [
        e.payload
        for e in runtime.events.list_events(replay.run_id)
        if e.category == C.MEMORY_SEARCH
    ]
    assert search["created_before"] is not None
