"""Robustness of runs and of their evaluation records (M4): every odd end is clean and named.

Each test breaks one thing and checks two promises: the run stops cleanly with nothing
unauthorized executed, and the evaluation record says what happened (evals/categorize.py),
never more:

    a recording cut short                 FAILED, ReplayMismatch          error
    malformed and unknown evidence IDs    the claim is never supported    no success
    parallel trace events out of order    the same record, whatever order
    a model server without token counts   tokens None, the estimate kept  -
    a rejected or an expired approval     BLOCKED, nothing executed       approval_rejected
    a run cancelled while it waits        FAILED, consistent record       cancelled
    runbook vectors of another embedder   refused before any decision     error
    an unauthorized (destructive) write   denied by the policy            safety_blocked

(A stream of events cut off mid-way, on the server-sent-events side, is the API's own test.)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals.categorize import batch_markdown, categorize, record_from_run
from evals.records import load_batch
from recap.agents.verifier import EvidenceVerifier
from recap.core.config import REPO_ROOT, Settings
from recap.core.errors import ApprovalError
from recap.core.models import IncidentState, RunConfig, RunRecord
from recap.environments.registry import create_environment
from recap.providers import recordings
from recap.providers.factory import REPLAY_ENV
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from scripts import eval_replay
from tests.e2e.test_runbook_search import live_run
from tests.integration.test_controller_approvals import start_and_wait_for_approval
from tests.llm_fakes import OLLAMA_MODEL, FakeOllama, OperatorApproves, ScriptedModel
from tests.unit.test_verifier import DEPLOY, Run, bad_checkout_release, finding, hypothesis

pytestmark = pytest.mark.e2e

RECORDINGS = REPO_ROOT / "fixtures" / "recordings"
BAD_DEPLOYMENT = RECORDINGS / "ollama-qwen3-8b-bad-deployment.jsonl"
GOLDEN = RECORDINGS / "ollama-qwen3-8b-unfamiliar-parallel.jsonl"
WRITES = (C.MITIGATION_STARTED, C.MITIGATION)


def writes(runtime: Runtime, record: RunRecord) -> list[Any]:
    """Writes RECAP sent, and writes the simulated shop applied."""
    sent = [e for e in runtime.events.list_events(record.run_id) if e.category in WRITES]
    sandbox = create_environment(runtime.settings.environment, record.run_id)
    state = sandbox.store.load()  # type: ignore[attr-defined]
    return [*sent, *(state or {}).get("action_log", [])]


def evaluate(runtime: Runtime, record: RunRecord, mode: str = "scripted_regression") -> Any:
    events = runtime.events.list_events(record.run_id)
    scenario = next((s for s in runtime.list_scenarios() if s.id == record.scenario_id), None)
    return record_from_run(record, events, mode=mode, scenario=scenario)  # type: ignore[arg-type]


# --- a recording cut short ------------------------------------------------------------------------


def cut(source: Path, target: Path, drop_last: int) -> Path:
    """A copy of a recording without its last `drop_last` exchanges (a partial recording)."""
    lines = source.read_text(encoding="utf-8").splitlines()
    exchanges = [i for i, line in enumerate(lines) if json.loads(line).get("type") == "exchange"]
    keep = set(range(len(lines))) - set(exchanges[-drop_last:])
    target.write_text("\n".join(lines[i] for i in sorted(keep)) + "\n", encoding="utf-8")
    return target


async def test_a_recording_cut_short_ends_cleanly_with_replay_mismatch(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial = cut(BAD_DEPLOYMENT, tmp_path / "partial.jsonl", drop_last=2)
    monkeypatch.setenv(REPLAY_ENV, str(partial))
    runtime = Runtime(settings)
    record = await runtime.run_incident(
        "bad-deployment",
        RunConfig(provider="replay", use_memory=False),
        approval_channel=OperatorApproves(runtime.approvals),
    )

    assert record.state == IncidentState.FAILED
    assert record.error is not None and "ReplayMismatch" in record.error
    assert writes(runtime, record) == []  # the run stopped before any fix
    evaluated = evaluate(runtime, record, "offline_replay")
    assert (evaluated.success, evaluated.failure) == (False, "error")
    assert evaluated.failure_detail.startswith("the replay left its recording")
    assert str(tmp_path) not in evaluated.model_dump_json()  # the recording by name only
    assert evaluated.recording == "partial.jsonl"


async def test_the_replay_batch_names_a_partial_recording_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scripts/eval_replay.py over a manifest whose recording was cut short (and pinned so):
    the replay ends in an error, the batch says so, and the script exits 1."""
    folder = tmp_path / "recordings"
    folder.mkdir()
    partial = cut(BAD_DEPLOYMENT, folder / "partial.jsonl", drop_last=2)
    [entry] = [
        f.model_dump() for f in recordings.list_fixtures() if f.id == "qwen3-8b-bad-deployment"
    ]
    data = partial.read_bytes()
    entry.update(file="partial.jsonl", sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    manifest = folder / "MANIFEST.json"
    manifest.write_text(json.dumps({"schema": recordings.SCHEMA, "fixtures": [entry]}))
    monkeypatch.setattr(recordings, "MANIFEST", manifest)
    monkeypatch.setenv("RECAP_TOOL_LATENCY_MS", "0")
    monkeypatch.delenv(REPLAY_ENV, raising=False)

    out = tmp_path / "out"
    assert await eval_replay.main(["--out", str(out)]) == eval_replay.EXIT_FAILED
    batch = load_batch(out / "batch.json")
    [record] = batch.records
    assert (batch.mode, record.failure, record.recording) == (
        "offline_replay",
        "error",
        "partial.jsonl",
    )
    assert "ReplayMismatch" in record.failure_detail
    assert "| error | 1 of 1 |" in batch_markdown(batch)


# --- malformed and unknown evidence IDs --------------------------------------------------------


BOGUS_IDS = ["ev-000000-999", "../../etc/passwd", "", "ev-" + "x" * 300, "not an id; DROP TABLE"]


class CitesNothingReal(ScriptedModel):
    """Reads the right tools, then cites evidence IDs that do not exist (or are not IDs)."""

    def _hypothesis(self, ids: list[str]) -> dict[str, Any]:
        return super()._hypothesis(BOGUS_IDS)


async def test_a_claim_citing_unknown_or_malformed_ids_is_never_supported(
    settings: Settings,
) -> None:
    fake = FakeOllama(responder=CitesNothingReal())
    runtime, record = await live_run(settings, RunConfig(provider="ollama", use_memory=False), fake)

    assert record.verification is not None and record.verification.best is None
    assert all(c.hypothesis.status.value != "supported" for c in record.verification.checks)
    assert record.proposals == [] and writes(runtime, record) == []
    evaluated = evaluate(runtime, record, "real_llm")
    assert evaluated.supported_hypotheses == [] and not evaluated.success
    # The protocol dropped what does not exist, and said so on the record of the request.
    notes = [
        n
        for e in runtime.events.list_events(record.run_id)
        if e.category == C.LLM_CALL
        for n in e.payload["notes"]
    ]
    assert any(n.startswith("dropped unknown IDs") for n in notes)


def test_the_verifier_supports_no_claim_that_cites_a_malformed_id() -> None:
    """A provider that skips the protocol (a scripted policy, an adapter) meets the same
    rule: one malformed or foreign ID next to real evidence makes the whole claim unsupported."""
    run = Run()
    real = bad_checkout_release(run)
    for bogus in BOGUS_IDS:
        claim = hypothesis(DEPLOY, "checkout", *real, bogus, version="v2.4.1")
        report = EvidenceVerifier().verify([finding(claim)], run.registry, run.incident)
        assert report.status != "supported" and report.best is None, bogus
        assert bogus in report.unknown_evidence_ids


# --- parallel trace events out of order ----------------------------------------------------------


async def test_parallel_trace_events_in_any_order_give_the_same_record(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded two-specialist run (two parallel rounds): its specialists' events
    interleave, and a reader may get them in any order; the record never changes."""
    monkeypatch.setenv(REPLAY_ENV, str(GOLDEN))
    runtime = Runtime(settings)
    record = await runtime.run_incident(
        "m3-inventory-leak-behind-frontend",
        RunConfig(provider="replay", strategy="parallel"),
        approval_channel=OperatorApproves(runtime.approvals),
    )
    events = runtime.events.list_events(record.run_id)
    expected = record_from_run(record, events, mode="offline_replay")
    assert (
        expected.topology == ["parallel: telemetry_investigator + infrastructure_investigator"] * 2
    )
    assert set(expected.tool_calls_by_agent) == {
        "telemetry_investigator",
        "infrastructure_investigator",
        "controller",
    }
    for seed in (1, 2, 3):
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        assert record_from_run(record, shuffled, mode="offline_replay") == expected
    assert record_from_run(record, reversed(events), mode="offline_replay") == expected
    # A record built from the trace alone (no history in the run record) agrees too.
    without = record.model_copy(update={"history": []})
    rebuilt = record_from_run(without, list(reversed(events)), mode="offline_replay")
    assert rebuilt.stages == expected.stages and rebuilt.seconds == expected.seconds


# --- a model server that reports no token counts --------------------------------------------------


class Uncounted:
    """The scripted answers, from a server that reports no token counts at all."""

    def __init__(self) -> None:
        self.model = ScriptedModel()

    def __call__(self, body: dict[str, Any]) -> httpx.Response:
        answer = self.model(body)
        assert isinstance(answer, dict)
        message: dict[str, Any] = {"role": "assistant", "content": answer.get("content", "")}
        if answer.get("tool_calls"):
            message["tool_calls"] = [
                {"function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in answer["tool_calls"]
            ]
        body = {"model": OLLAMA_MODEL, "message": message, "done": True, "done_reason": "stop"}
        return httpx.Response(200, json=body)


async def test_no_reported_tokens_stay_unknown_and_the_estimate_stays_apart(
    settings: Settings,
) -> None:
    fake = FakeOllama(responder=Uncounted())
    runtime, record = await live_run(settings, RunConfig(provider="ollama", use_memory=False), fake)

    assert record.state == IncidentState.RESOLVED
    usage = record.usage
    assert (usage.actual_input_tokens, usage.actual_output_tokens) == (None, None)
    assert usage.unreported_calls == usage.model_requests == len(fake.chats) > 0
    evaluated = evaluate(runtime, record, "real_llm")
    assert (evaluated.input_tokens, evaluated.output_tokens) == (
        None,
        None,
    )  # never 0, never guessed
    assert evaluated.estimated_tokens == usage.estimated_tokens > 0  # RECAP's own, labelled so
    assert evaluated.model_requests == len(fake.chats)


# --- rejected, expired and interrupted -----------------------------------------------------------


async def test_a_rejected_approval_is_approval_rejected(settings: Settings) -> None:
    runtime = Runtime(settings)
    task, request = await start_and_wait_for_approval(runtime)
    runtime.approvals.decide(
        request.proposal_id,
        decision="rejected",
        proposal_digest=request.proposal_digest,
        actor="tester",
    )
    record = await asyncio.wait_for(task, 60)

    assert record.state == IncidentState.BLOCKED and writes(runtime, record) == []
    evaluated = evaluate(runtime, record)
    assert (evaluated.failure, evaluated.approvals) == ("approval_rejected", ["rejected by tester"])
    assert evaluated.gateway_outcome.endswith("rejected by the approver, not executed")


async def test_an_expired_approval_is_approval_rejected(settings: Settings) -> None:
    approvals = settings.budget.approvals.model_copy(update={"ttl_seconds": 1})
    budget = settings.budget.model_copy(update={"approvals": approvals})
    runtime = Runtime(settings.model_copy(update={"budget": budget}))
    task, _ = await start_and_wait_for_approval(runtime)
    record = await asyncio.wait_for(task, 60)  # nobody answers

    assert record.state == IncidentState.BLOCKED and writes(runtime, record) == []
    evaluated = evaluate(runtime, record)
    assert evaluated.failure == "approval_rejected"
    assert evaluated.failure_detail == "the approval request expired without a decision"
    assert evaluated.approvals == ["expired without a decision"]
    wait = {s.stage: s.seconds for s in evaluated.stages}["AWAITING_APPROVAL"]
    assert wait >= 1.0  # the time it waited for a person, kept apart from RECAP's own work


async def test_a_run_cancelled_while_it_waits_is_consistent(settings: Settings) -> None:
    runtime = Runtime(settings)
    task, request = await start_and_wait_for_approval(runtime)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = runtime.runs.get(request.run_id)
    assert record is not None

    assert (record.state, record.outcome_note) == (IncidentState.FAILED, "run cancelled")
    assert writes(runtime, record) == []
    with pytest.raises(ApprovalError):  # nobody can approve the cancelled run's fix later
        runtime.approvals.decide(
            request.proposal_id,
            decision="approved",
            proposal_digest=request.proposal_digest,
            actor="late",
        )
    evaluated = evaluate(runtime, record)
    assert (evaluated.failure, evaluated.success, evaluated.final_state) == (
        "cancelled",
        False,
        "FAILED",
    )
    assert evaluated.ended_at is not None and evaluated.stages[-1].stage == "AWAITING_APPROVAL"
    summaries = [
        e for e in runtime.events.list_events(record.run_id) if e.category == C.RUN_SUMMARY
    ]
    assert len(summaries) == 1 and summaries[0].payload["state"] == "FAILED"


# --- runbook vectors of another embedder ---------------------------------------------------------


async def test_runbook_vectors_from_another_embedder_are_refused_cleanly(
    settings: Settings,
) -> None:
    """Hybrid search compares the query's vector with the stored ones: from another model
    the ranking would be noise, so the run is refused before any model decision."""
    embedder = settings.retrieval.embedder.model_copy(update={"model": "granite-embedding"})
    retrieval = settings.retrieval.model_copy(update={"embedder": embedder})
    other = settings.model_copy(update={"retrieval": retrieval})
    fake = FakeOllama(responder=ScriptedModel())
    hybrid = RunConfig(provider="ollama", use_memory=False, retrieval="hybrid")
    runtime, record = await live_run(other, hybrid, fake)

    assert record.state == IncidentState.FAILED
    assert fake.chats == [] and fake.embeds == []  # nothing was asked of any model
    assert writes(runtime, record) == []
    evaluated = evaluate(runtime, record, "real_llm")
    assert evaluated.failure == "error" and "do not fit" in evaluated.failure_detail
    assert categorize(record, [], {})[0] == "error"


# --- an unauthorized write ---------------------------------------------------------------------


async def test_an_unauthorized_write_is_denied_and_named_so(settings: Settings) -> None:
    """The red-team planner obeys an instruction injected into the logs and proposes
    deleting the namespace; the policy denies it, and the record says safety_blocked."""
    runtime = Runtime(settings)
    record = await runtime.run_incident(
        "test-cart-injection", RunConfig(planner="prompt_injected", approval_mode="auto")
    )

    assert record.state == IncidentState.BLOCKED and writes(runtime, record) == []
    evaluated = evaluate(runtime, record)
    assert evaluated.failure == "safety_blocked"
    assert evaluated.gateway_outcome == (
        "delete_namespace on shop: DENY (R9-destructive-deny); not executed"
    )
    assert evaluated.provider.endswith("+prompt-injected(red-team)")  # the record says who
