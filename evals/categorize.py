"""Evaluation records from finished runs, and why a run did not succeed (M4).

    RunRecord + its trace + the scenario's predeclared answer
        |  record_from_run(...)
        v
    EvalRecord (evals/records.py)
        success   True ONLY when the independent oracle verified the recovery
        failure   categorize(...): one category for any other end

Every value comes from what the run itself recorded: its RunRecord (state, usage, proposals,
approvals, the oracle's verdict) and its trace (state transitions, tool calls, model
requests, the verifier's verdicts). Nothing is estimated here: a value the run did not
record stays empty (None, 0, "" or []). Model text never decides anything; the rules read
lifecycle states, rule verdicts, tool arguments and enum values.

The scenario's `expected` answer is predeclared in its file and never reaches a model (ADR
0010). It is read here only after the run, to tell an unsolved investigation's failures
apart. The rules are generic: no scenario, service or incident name appears in them.

The categories, checked in this order (the first that matches wins):

    ""                     RESOLVED, and the oracle verified the recovery (RESOLVED without
                           that verdict is an error, never a success)
    cancelled              someone stopped the run
    quota_cap              a free plan's (or RECAP's own) rate cap deferred it
    budget_exhausted       a run limit stopped it (steps, tool calls, tokens, time)
    approval_rejected      BLOCKED: the approver said no, or the approval request expired
    safety_blocked         BLOCKED: the policy, the evidence gate or the namespace allowlist
  then, for a FAILED run:
    error                  a replay that left its recording (ReplayMismatch): the recording,
                           not the model, no longer matches RECAP
    oracle_not_recovered   a fix ran, and the oracle found the system still unhealthy
    budget_exhausted       every agent of the last round ran out of its time share
    provider_failure       the model server failed (a provider error ended the run)
    error                  any other error ended the run
    open_disagreement      supported causes (or releases) still conflicted: a person decides
    provider_failure       no agent got a usable answer, or no usable fix came back for a
                           supported diagnosis
  and, when the investigation ended without a supported root cause, judged against the
  predeclared root service R and mechanism M:
    missed_crucial_tool    no tool call ever named R
    wrong_mechanism        claims named R, but none with M
    wrong_service          the claims put the fault elsewhere (none names R, even as the
                           failing dependency of a link the verifier did not contradict)
    insufficient_evidence  everything else: the right claim stayed unsupported, the claims
                           traced the failure to R without concluding its own fault, or no
                           usable claim was made
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from evals.records import (
    EvalBatch,
    EvalMode,
    EvalRecord,
    FailureCategory,
    StageTiming,
    write_batch,
)
from recap.core.config import REPO_ROOT
from recap.core.models import (
    TERMINAL_STATES,
    IncidentState,
    RunRecord,
    ScenarioInfo,
    StateChange,
    TraceEvent,
)
from recap.telemetry.redact import Redactor
from recap.telemetry.tracer import TraceCategory as C

# Published batches (in git): evals/reports/batches/<batch_id>/batch.json, and README.md: the
# batch as people read it (GitHub shows it when the folder is opened).
PUBLISHED_DIR = REPO_ROOT / "evals" / "reports" / "batches"

# Where each mode's runs must come from (ProviderInfo.origin). A record whose run came from
# anywhere else is refused, so one mode's numbers can never hide another's.
MODE_ORIGINS: dict[str, frozenset[str]] = {
    "scripted_regression": frozenset({"scripted"}),
    "offline_replay": frozenset({"replay"}),
    "real_llm": frozenset({"local_llm", "free_hosted_llm"}),
}

# Plain names for people (summaries, the dashboard's tabs).
MODE_LABELS: dict[str, str] = {
    "scripted_regression": "Scripted regression (the scripted policy, not an LLM)",
    "offline_replay": "Offline replay of recorded real-model runs (no model is called)",
    "real_llm": "Real model on simulated incidents",
    "kubernetes_future": "Real cluster (Kind or SREGym): M5, not implemented",
}

# The controller's own note for a cancelled run (recap/harness/controller.py, run()).
CANCELLED_NOTE = "run cancelled"
# Error types that mean the model server failed (recap/core/errors.py). A ReplayMismatch is a
# ProviderError too, but in a replay it means the RECORDING no longer matches: an error.
_PROVIDER_ERROR = re.compile(r"\b(ProviderError|FreeStatusLost)\b")
_REPLAY_MISMATCH = re.compile(r"\bReplayMismatch\b")
# An agent stopped by its own time share (recap/routing/orchestrator.py, _error_text).
_TIMEOUT = "timeout after"
# Results of an action the target received (recap/harness/controller.py, SENT_RESULTS).
_SENT = frozenset({"recovered", "not_recovered", "error"})
# A disagreement that hands the run to a person when it is still open (controller._handover).
_HANDOVER_KINDS = frozenset({"competing_root_causes", "detail_conflict"})
DETAIL_CHARS = 300  # how much of an error or note a record keeps

_redactor = Redactor()
# Folders a local path starts with: a user's own (Users, home) and the system's.
_USER_ROOTS = ("Users", "home")
_LOCAL_ROOTS = "Users|home|private|tmp|var|Volumes|root|mnt|opt|srv"
# A local path inside free text (an error message): "/Users/x/y.jsonl" or "~/x/y.jsonl".
# API routes ("/api/chat") and URLs ("http://127.0.0.1:11434/api/chat") are left alone.
_PATH = re.compile(rf"(?<![\w:/.~-])(?:~|/(?:{_LOCAL_ROOTS}))(?=/|\b)(?:/[^\s/'\"(),;]+)*/?")
# What must never reach a published batch: a path into a user's or the system's folders.
_LOCAL_FOLDER = re.compile(rf"(?<![\w:/.~-])/(?:{_LOCAL_ROOTS})/")


# --- one run -> one record --------------------------------------------------------------------


def record_from_run(
    record: RunRecord,
    events: Iterable[TraceEvent],
    *,
    mode: EvalMode,
    scenario: ScenarioInfo | None = None,
    expected: Mapping[str, Any] | None = None,
    recording: str | None = None,
    service_args: Collection[str] = ("service",),
) -> EvalRecord:
    """The evaluation record of one finished run, from its record and its trace only.

    `scenario` gives the incident family, the split and the predeclared answer (`expected`
    replaces that answer when given). `recording` names the run's recording by its id or
    file name; by default it is the file name of the recording the run itself names (a
    local path is never kept). `service_args` are the tool arguments that name a service
    (configs/policies.yaml names every one "service").

    Raises ValueError for a run of another origin than `mode` allows (a scripted run is never
    a real-model record, a replay never a live one), for the kubernetes_future mode, which
    nothing produces (M5), and for a run against a target that is not simulated.
    """
    info = record.provider_info
    origin = info.origin if info else str(record.versions.get("provider_origin", ""))
    check_mode(mode, origin)
    if record.environment is not None and not record.environment.is_simulated:
        raise ValueError(
            f"{record.run_id} ran against {record.environment.kind!r}, not the simulated "
            "environment: a real cluster is M5 (kubernetes_future), which is not implemented"
        )
    trace = _ordered(events, record.run_id)
    answer = dict(expected if expected is not None else (scenario.expected if scenario else {}))
    failure, detail = categorize(record, trace, answer, service_args=service_args)
    history = _history(record, trace)
    verified = bool(record.recovery and record.recovery.verified)
    by_agent = tool_calls_by_agent(trace)
    diagnosis = _diagnosis(record)
    return EvalRecord(
        run_id=record.run_id,
        mode=mode,
        scenario_id=record.scenario_id,
        incident_id=record.incident.incident_id if record.incident else "",
        incident_family=scenario.family if scenario else "",
        split=scenario.partition.value if scenario else "",
        expected=answer,
        provider=info.name if info else record.provider,
        provider_class=origin,
        model=info.model if info else None,
        model_weights=_weights(record),
        prompt_version=info.prompt_version if info else None,
        config_hash=str(record.versions.get("config_hash", "")),
        code_version=_text_or_none(record.versions.get("code_version")),
        strategy=record.config.strategy,
        topology=topology(record, trace),
        rounds=len(record.strategies) or len(_rounds(trace)),
        retrieval_mode=_retrieval_mode(record),
        memory=record.config.use_memory,
        model_requests=record.usage.model_requests,
        input_tokens=record.usage.actual_input_tokens,
        output_tokens=record.usage.actual_output_tokens,
        estimated_tokens=record.usage.estimated_tokens,
        tool_calls=sum(by_agent.values()),
        tool_calls_by_agent=by_agent,
        retrieved_docs=_retrieved_docs(record, trace),
        agent_searches=_agent_searches(record, trace),
        memory_hits=_memory_hits(record),
        supported_hypotheses=supported_hypotheses(record, trace),
        diagnosis=diagnosis,
        diagnosis_correct=_diagnosis_correct(diagnosis, answer),
        proposed_actions=[_proposal_text(p) for p in record.proposals],
        gateway_outcome=gateway_outcome(record, trace),
        approvals=approval_lines(record, trace),
        final_state=record.state.value,
        oracle_verified=verified,
        success=record.state == IncidentState.RESOLVED and verified,
        failure=failure,
        failure_detail=detail,
        seconds=_total_seconds(history, record),
        stages=stage_timings(history),
        model_seconds=model_timing(trace).seconds,
        started_at=history[0].at if history else record.created_at,
        ended_at=history[-1].at if history and record.state in TERMINAL_STATES else None,
        recording=recording if recording is not None else recording_name(record),
    )


def check_mode(mode: str, origin: str) -> None:
    """Refuse a run whose decisions came from somewhere `mode` does not cover."""
    if mode == "kubernetes_future":
        raise ValueError(
            "kubernetes_future is the M5 real-cluster mode and is not implemented: no record "
            "may carry it"
        )
    allowed = MODE_ORIGINS.get(mode)
    if allowed is None:
        raise ValueError(f"unknown evaluation mode {mode!r}")
    if origin not in allowed:
        raise ValueError(
            f"a {mode} record needs a run from {' or '.join(sorted(allowed))}, but this run's "
            f"decisions came from {origin or 'an unknown origin'!r}"
        )


# --- why a run did not succeed ------------------------------------------------------------------


def categorize(
    record: RunRecord,
    events: Iterable[TraceEvent],
    expected: Mapping[str, Any],
    *,
    service_args: Collection[str] = ("service",),
) -> tuple[FailureCategory, str]:
    """The failure category of one run and a short reason ("" and a note for a success).

    Deterministic: the same record, trace and expected answer always give the same result,
    whatever order the trace events arrive in.
    """
    trace = _ordered(events, record.run_id)
    note = clean_text(record.outcome_note)
    state = record.state
    if state == IncidentState.RESOLVED and record.recovery and record.recovery.verified:
        return "", note or "the oracle verified the recovery"
    if state == IncidentState.RESOLVED:  # never a success without the oracle's verdict
        return "error", "the run says RESOLVED, but the oracle has not verified the recovery"
    if note == CANCELLED_NOTE:
        return "cancelled", "the run was cancelled before it finished"
    if state == IncidentState.QUOTA_EXHAUSTED:
        return "quota_cap", note
    if state == IncidentState.BUDGET_EXHAUSTED:
        return "budget_exhausted", _budget_detail(trace) or note
    if state == IncidentState.BLOCKED:
        return _blocked(record)
    if state not in TERMINAL_STATES:
        return "error", f"the run did not finish (last state {state.value})"
    # FAILED from here. A replay that left its recording says nothing about the model: the
    # recording no longer matches RECAP, whatever else happened in the run.
    if record.error and _REPLAY_MISMATCH.search(record.error):
        return "error", clean_text(f"the replay left its recording: {record.error}")
    # A fix that ran without recovering the system is the decisive fact, even when a later
    # round then failed too (the detail keeps both).
    if record.recovery is not None and not record.recovery.verified:
        sent = [a for a in record.actions if a.result in _SENT]
        fixes = ", ".join(f"{a.action} on {a.target}" for a in sent) or "a fix"
        later = f"; then: {record.error}" if record.error else ""
        return "oracle_not_recovered", clean_text(
            f"{fixes} ran; the oracle found the system still unhealthy{later}"
        )
    stopped = _stopped_agents(record)
    if stopped is not None:
        return stopped
    if record.error:
        return _error(record.error)
    report = record.verification
    if report is not None:
        open_ = [
            d
            for d in report.disagreements
            if not d.resolved and d.question and d.kind in _HANDOVER_KINDS
        ]
        if report.best is None and open_:
            claims = "; ".join(c for d in open_ for c in d.claims)
            return "open_disagreement", clean_text(f"still in conflict: {claims}")
    findings = [f for f in record.findings if not f.partial]
    if findings and all(f.outcome in ("no_usable_answer", "error") for f in findings):
        return "provider_failure", "no agent got a usable answer from the model"
    if report is not None and report.best is not None and not record.proposals:
        why = _unusable_proposal(trace)
        return "provider_failure", clean_text(
            "no usable fix was proposed for the supported diagnosis" + (f" ({why})" if why else "")
        )
    return _unsolved(record, trace, expected, service_args)


def _unusable_proposal(trace: Sequence[TraceEvent]) -> str:
    """RECAP's note on the last proposal that could not be used (the parser's own words)."""
    notes = [
        str(note)
        for event in trace
        if event.category == C.PROPOSAL and not event.payload.get("proposal")
        for note in event.payload.get("notes") or []
    ]
    return notes[-1] if notes else ""


def _blocked(record: RunRecord) -> tuple[FailureCategory, str]:
    """BLOCKED: refused by the approver (or no decision in time), or by a guardrail."""
    for action in record.actions:
        if action.result == "rejected":
            return "approval_rejected", clean_text(action.note or "the approver said no")
        if action.result == "expired":
            return "approval_rejected", "the approval request expired without a decision"
    return "safety_blocked", clean_text(record.outcome_note)


def _stopped_agents(record: RunRecord) -> tuple[FailureCategory, str] | None:
    """Every agent of the last round ended with an error: a time share or a provider failure.

    The controller ends such a run FAILED ("every agent in the round failed"); what the
    agents' errors say decides the category: all ran out of their time share is a run
    limit; any provider error is the model server's failure.
    """
    findings = [f for f in record.findings if not f.partial]
    if not findings:
        return None
    last = max(f.round for f in findings)
    errors = [f.error for f in findings if f.round == last]
    if not errors or not all(errors):
        return None
    text = "; ".join(e for e in errors if e)
    if all(e and e.startswith(_TIMEOUT) for e in errors):
        return "budget_exhausted", clean_text(f"every agent ran out of its time share ({text})")
    if _PROVIDER_ERROR.search(text) and not _REPLAY_MISMATCH.search(text):
        return "provider_failure", clean_text(text)
    return None


def _error(error: str) -> tuple[FailureCategory, str]:
    """A run that ended with an error: the model server's failure, or anything else."""
    if _PROVIDER_ERROR.search(error):
        return "provider_failure", clean_text(error)
    return "error", clean_text(error)


def _unsolved(
    record: RunRecord,
    trace: Sequence[TraceEvent],
    expected: Mapping[str, Any],
    service_args: Collection[str],
) -> tuple[FailureCategory, str]:
    """No supported root cause: compare what the agents did with the predeclared answer."""
    root = str(expected.get("root_cause_service") or "")
    mechanism = str(expected.get("mechanism") or "")
    if not root or not mechanism:
        return "insufficient_evidence", "no supported root cause (the scenario declares none)"
    looked = {
        str(value)
        for event in trace
        if event.category == C.TOOL_CALL
        for key, value in (event.payload.get("args") or {}).items()
        if key in service_args
    }
    if root not in looked:
        named = ", ".join(sorted(looked)) or "no service"
        return "missed_crucial_tool", f"no tool call named {root}; the calls named {named}"
    claims = [h for f in record.findings for h in f.hypotheses]
    on_root = [h for h in claims if h.target_service == root]
    if on_root and not any(h.mechanism.value == mechanism for h in on_root):
        said = ", ".join(sorted({h.mechanism.value for h in on_root}))
        return "wrong_mechanism", f"the claims on {root} named {said}, not {mechanism}"
    # A dependency claim ("X fails because the root service fails") traces the failure to the
    # root service, unless the verifier contradicted that link: a wrong link traces nothing.
    broken = _contradicted_links(record, trace)
    toward_root = [
        h
        for h in claims
        if h.details.get("dependency") == root and (h.target_service, root) not in broken
    ]
    if claims and not on_root and not toward_root:
        targets = ", ".join(sorted({h.target_service or "no service" for h in claims}))
        return "wrong_service", f"the claims named {targets}; none named {root}"
    right = [h for h in on_root if h.mechanism.value == mechanism]
    if right:
        verdicts = _claim_verdicts(record, mechanism, root)
        return "insufficient_evidence", (
            f"{mechanism} on {root} was claimed, but the rules found it {verdicts}"
        )
    if toward_root:
        return "insufficient_evidence", (
            f"the claims traced the failure to {root}, but none concluded {root}'s own fault"
        )
    return "insufficient_evidence", "no usable claim was made"


def _contradicted_links(record: RunRecord, trace: Sequence[TraceEvent]) -> set[tuple[Any, str]]:
    """(caller, dependency) pairs of dependency claims the verifier contradicted in any round."""
    links: set[tuple[Any, str]] = set()
    for event in trace:
        if event.category != C.VERIFY_EVIDENCE:
            continue
        for check in event.payload.get("checks") or []:
            if check.get("mechanism") == "dependency_failure" and (
                check.get("status") == "contradicted"
            ):
                links.add((check.get("target"), str(check.get("detail") or "")))
    report = record.verification
    for check in report.checks if report else []:
        claim = check.hypothesis
        if claim.mechanism.value == "dependency_failure" and claim.status.value == "contradicted":
            links.add((claim.target_service, check.detail))
    return links


def _claim_verdicts(record: RunRecord, mechanism: str, target: str) -> str:
    report = record.verification
    statuses = sorted(
        {
            c.hypothesis.status.value
            for c in (report.checks if report else [])
            if c.hypothesis.mechanism.value == mechanism and c.hypothesis.target_service == target
        }
    )
    return " or ".join(statuses) or "unsupported"


def _budget_detail(trace: Sequence[TraceEvent]) -> str:
    stops = [e.payload for e in trace if e.category == C.BUDGET]
    if not stops:
        return ""
    last = stops[-1]
    return clean_text(f"limit {last.get('limit')}: {last.get('detail') or ''}".rstrip(": "))


# --- parts of the record --------------------------------------------------------------------------


def stage_timings(history: Sequence[StateChange]) -> list[StageTiming]:
    """Seconds spent in each lifecycle state, summed over repeats, in first-seen order.

    AWAITING_APPROVAL is its own stage: the time a person (or the simulation auto-approver)
    took to decide, kept apart from the time RECAP itself worked. The terminal state has no
    duration and is left out.
    """
    totals: dict[str, float] = {}
    for change, following in zip(history, history[1:], strict=False):
        seconds = max(0.0, (following.at - change.at).total_seconds())
        totals[change.state.value] = totals.get(change.state.value, 0.0) + seconds
    return [StageTiming(stage=stage, seconds=round(value, 3)) for stage, value in totals.items()]


@dataclass(frozen=True)
class ModelTiming:
    """How long the run's model requests took, as the client and the server measured them.

    `seconds` sums every request's duration as RECAP saw it (sending to answer). When two
    agents ask one local server that answers one request at a time, the second request waits
    in its queue, so the sum counts that wait too. `server_seconds` sums the server's own
    processing time where it reported one (Ollama's total_duration, ModelCall.server_ms);
    `waiting_seconds` is the difference over those requests: time spent elsewhere, mostly
    queued. Replayed answers are left out: no model ran for them.
    """

    requests: int
    seconds: float | None
    server_requests: int  # requests that reported the server's own time
    server_seconds: float | None
    waiting_seconds: float | None


def model_timing(events: Iterable[TraceEvent]) -> ModelTiming:
    calls = [
        e.payload for e in events if e.category == C.LLM_CALL and not e.payload.get("replayed")
    ]
    durations = [float(c.get("duration_ms") or 0.0) for c in calls]
    timed = [c for c in calls if isinstance(c.get("server_ms"), int | float)]
    server = sum(float(c["server_ms"]) for c in timed)
    client = sum(float(c.get("duration_ms") or 0.0) for c in timed)
    return ModelTiming(
        requests=len(calls),
        seconds=round(sum(durations) / 1000, 3) if calls else None,
        server_requests=len(timed),
        server_seconds=round(server / 1000, 3) if timed else None,
        waiting_seconds=round(max(0.0, client - server) / 1000, 3) if timed else None,
    )


def tool_calls_by_agent(events: Iterable[TraceEvent]) -> dict[str, int]:
    """Tool calls sent per caller: the agents' reads (RECAP's own runbook search included)
    and, as "controller", the writes sent to the target. Denied calls were never sent."""
    counts: dict[str, int] = {}
    for event in events:
        if event.category == C.TOOL_CALL:
            caller = str(event.payload.get("caller") or event.agent or "unknown")
        elif event.category == C.MITIGATION_STARTED:
            caller = "controller"
        else:
            continue
        counts[caller] = counts.get(caller, 0) + 1
    return counts


def topology(record: RunRecord, events: Sequence[TraceEvent] = ()) -> list[str]:
    """Each investigation round's routing: "parallel: telemetry_investigator + ..."."""
    if record.strategies:
        return [
            f"{d.mode.value}: {' + '.join(a.value for a in d.selected_agents)}"
            for d in record.strategies
        ]
    return [
        f"{payload.get('mode')}: {' + '.join(str(a) for a in payload.get('agents') or [])}"
        for payload in _rounds(events)
    ]


def supported_hypotheses(record: RunRecord, events: Sequence[TraceEvent]) -> list[str]:
    """Every claim the verifier supported in any round, as "mechanism@service"."""
    found: list[str] = []
    for event in events:
        if event.category != C.VERIFY_EVIDENCE or "checks" not in event.payload:
            continue
        for check in event.payload.get("checks") or []:
            if check.get("status") == "supported":
                found.append(f"{check.get('mechanism')}@{check.get('target')}")
    report = record.verification
    for check in report.checks if report else []:
        if check.hypothesis.status.value == "supported":
            found.append(f"{check.hypothesis.mechanism.value}@{check.hypothesis.target_service}")
    return list(dict.fromkeys(found))


def gateway_outcome(record: RunRecord, events: Sequence[TraceEvent]) -> str:
    """What RECAP's guardrails decided for each proposed fix, in order ("" without one).

    For example "restart_service on inventory: APPROVAL_REQUIRED (R11-reversible-approval);
    evidence check passed; executed". The verdict is the policy's for the proposal; the
    evidence check is the verifier's proposal check (VP rules); then what became of it.
    """
    verdicts = _proposal_verdicts(events)
    checks = {
        str(e.payload.get("proposal_id")): e.payload
        for e in events
        if e.category == C.VERIFY_EVIDENCE and "proposal_check" in e.payload
    }
    lines: list[str] = []
    for index, proposal in enumerate(record.proposals):
        parts = [verdicts.get(proposal.proposal_id) or _record_verdict(record, index)]
        check = checks.get(proposal.proposal_id)
        if check is not None:
            result = check.get("proposal_check") or {}
            if result.get("ok"):
                parts.append("evidence check passed")
            else:
                problems = result.get("problems") or ["no reason recorded"]
                enforced = "refused" if check.get("enforced", True) else "failed (not enforced)"
                parts.append(f"evidence check {enforced}: {problems[0]}")
        action = record.actions[index] if index < len(record.actions) else None
        parts.append(_action_outcome(action.result if action else None))
        lines.append(f"{proposal.action} on {proposal.target}: " + "; ".join(p for p in parts if p))
    return clean_text("; ".join(lines), limit=4 * DETAIL_CHARS)


def approval_lines(record: RunRecord, events: Sequence[TraceEvent]) -> list[str]:
    """Each approval decision: who decided, and how the decision log says it came."""
    notes = {
        str(e.payload.get("approval_id")): str(e.payload.get("note") or "")
        for e in events
        if e.category == C.APPROVAL_DECIDED and e.payload.get("approval_id")
    }
    lines = []
    for approval in record.approvals:
        line = f"{approval.decision} by {approval.actor}"
        note = notes.get(approval.approval_id)
        lines.append(f"{line} ({note})" if note else line)
    for event in events:
        if event.category != C.APPROVAL_DECIDED:
            continue
        decision = event.payload.get("decision")
        if decision == "expired":
            lines.append("expired without a decision")
        elif decision == "refused":
            lines.append(f"refused: {event.payload.get('reason', '')}".rstrip(": "))
    return [clean_text(line) for line in lines]


def recording_name(record: RunRecord) -> str | None:
    """The file name of the recording the run names: never the folder it is in."""
    info = record.provider_info
    raw = (info.recording if info else None) or record.versions.get("recording")
    return Path(str(raw)).name if raw else None


def clean_text(text: str, limit: int = DETAIL_CHARS) -> str:
    """Text safe to publish: secrets redacted, local paths cut to their last part, one line.

    Paths inside the repository stay repo-relative ("fixtures/recordings/x.jsonl"); any other
    path keeps only its file name, and a path that is no more than a user's folder becomes
    "a local folder", so no user name survives.
    """
    text = _redactor.redact_text(str(text or ""))
    text = text.replace(str(REPO_ROOT) + "/", "").replace(str(Path.home()), "~")
    text = _PATH.sub(_last_part, text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _last_part(match: re.Match[str]) -> str:
    """A local path's file name; "a local folder" when that name could be a user's."""
    parts = [part for part in match.group(0).split("/") if part]
    if parts == ["~"]:
        return "~"
    if parts[0] == "~":
        return parts[-1]
    shortest = 3 if parts[0] in _USER_ROOTS else 2  # "/Users/<name>" alone names a user
    return parts[-1] if len(parts) >= shortest else "a local folder"


# --- a batch for people ------------------------------------------------------------------------


def publish_problems(batch: EvalBatch) -> list[str]:
    """Why a batch must not be published (it would leave this machine): [] when it may."""
    text = batch.model_dump_json()
    problems = []
    if str(Path.home()) in text or str(REPO_ROOT) in text or _LOCAL_FOLDER.search(text):
        problems.append("it names a local folder")
    if _redactor.redact_text(text) != text:
        problems.append("it holds a value shaped like a secret")
    return problems


def publish_batch(batch: EvalBatch, root: Path = PUBLISHED_DIR) -> Path:
    """Write <root>/<batch_id>/batch.json and its readable digest (README.md) next to it.

    Fail closed: a batch that names a local folder or holds a secret-shaped value is
    refused (ValueError) and nothing is written. Records keep file names, never paths, so
    this should never trigger; it is the last check before a batch can reach git.
    """
    problems = publish_problems(batch)
    if problems:
        raise ValueError(f"refused to publish batch {batch.batch_id}: {'; '.join(problems)}")
    path = write_batch(batch, root)
    (path.parent / "README.md").write_text(batch_markdown(batch), encoding="utf-8")
    return path


def outcome_counts(records: Sequence[EvalRecord]) -> dict[str, int]:
    """How many records ended in each category ("success" for verified recoveries)."""
    counts: dict[str, int] = {}
    for record in records:
        key = "success" if record.success else (record.failure or "error")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def batch_markdown(batch: EvalBatch) -> str:
    """A readable digest of one batch. Every number is a count of its records, never a rate."""
    records = batch.records
    lines = [
        f"# {batch.title}",
        "",
        f"> **{MODE_LABELS.get(batch.mode, batch.mode)}. SIMULATED target environment.** "
        f"N = {batch.n}: counts, not rates; no statistical claim.",
        "",
        f"- Batch `{batch.batch_id}` (mode `{batch.mode}`), created {batch.created_at.isoformat()}",
        f"- Plan: {_code(batch.plan) if batch.plan else 'none (see methods)'}",
        f"- Providers: {_codes(batch.providers)}; models: {_codes(batch.models)}; prompts: "
        f"{_codes(batch.prompt_versions)}",
        "",
        "## Outcomes",
        "",
        "| outcome | runs |",
        "|---|---|",
    ]
    lines += [f"| {key} | {count} of {batch.n} |" for key, count in outcome_counts(records).items()]
    lines += ["", "## Runs", ""]
    lines += [
        "| run | scenario | rounds (agents) | retrieval | memory | state | outcome | oracle "
        "| model requests | tokens in / out (reported) | est. tokens | tool calls | run seconds "
        "(approval wait) | model seconds |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        tokens = (
            f"{r.input_tokens:,} / {r.output_tokens or 0:,}"
            if r.input_tokens is not None
            else "not reported"
        )
        wait = sum(s.seconds for s in r.stages if s.stage == IncidentState.AWAITING_APPROVAL.value)
        outcome = "success" if r.success else r.failure
        model = f"{r.model_seconds:.1f}" if r.model_seconds is not None else "-"
        lines.append(
            f"| `{r.run_id}` | `{r.scenario_id}` | {_cell(_short_topology(r))} | "
            f"{r.retrieval_mode or '-'} | {'on' if r.memory else 'off'} | {r.final_state} | "
            f"{outcome} | {'verified' if r.oracle_verified else 'not verified'} | "
            f"{r.model_requests} | {tokens} | {r.estimated_tokens:,} | {r.tool_calls} | "
            f"{r.seconds:.1f} ({wait:.1f}) | {model} |"
        )
    lines += ["", "## Why each run ended as it did", ""]
    for r in records:
        detail = _cell(r.failure_detail) if r.failure_detail else "-"
        diagnosis = f"`{r.diagnosis}`" if r.diagnosis else "none"
        lines.append(
            f"- `{r.run_id}` ({r.scenario_id}): **{'success' if r.success else r.failure}**: "
            f"{detail}. Diagnosis {diagnosis}; fix: {_cell(r.gateway_outcome) or 'none proposed'}."
        )
    lines += ["", "## Methods", ""] + [f"- {m}" for m in batch.methods]
    lines += ["", "## Limitations (what these numbers do not show)", ""]
    lines += [f"- {m}" for m in batch.limitations]
    return "\n".join(lines) + "\n"


def _short_topology(record: EvalRecord) -> str:
    """'parallel: telemetry_investigator + infrastructure_investigator' as
    'parallel (telemetry + infrastructure)', one entry per round."""
    rounds = []
    for entry in record.topology or [record.strategy]:
        mode, _, agents = entry.partition(": ")
        names = " + ".join(a.removesuffix("_investigator") for a in agents.split(" + ") if a)
        rounds.append(f"{mode} ({names})" if names else mode)
    return "; ".join(rounds)


# --- small helpers --------------------------------------------------------------------------------


def _ordered(events: Iterable[TraceEvent], run_id: str) -> list[TraceEvent]:
    """This run's events in the order they were emitted (by sequence number), whatever order
    they arrive in: parallel agents interleave, and a reader may hand them over unsorted."""
    return sorted((e for e in events if e.run_id == run_id), key=lambda e: (e.seq, e.event_id))


def _history(record: RunRecord, trace: Sequence[TraceEvent]) -> list[StateChange]:
    """The run's state changes: from its record, or from its trace when the record has none."""
    if record.history:
        return sorted(record.history, key=lambda change: change.at)
    changes = [
        StateChange(
            state=IncidentState(e.payload["to"]), at=e.ts, reason=str(e.payload.get("reason", ""))
        )
        for e in trace
        if e.category == C.STATE and e.payload.get("to") in IncidentState.__members__
    ]
    return sorted(changes, key=lambda change: change.at)


def _total_seconds(history: Sequence[StateChange], record: RunRecord) -> float:
    if not history:
        return 0.0
    end = history[-1].at if record.state in TERMINAL_STATES else record.updated_at
    return round(max(0.0, (end - history[0].at).total_seconds()), 3)


def _rounds(events: Iterable[TraceEvent]) -> list[dict[str, Any]]:
    rounds = {
        int(e.payload.get("round") or 0): e.payload for e in events if e.category == C.ROUTING
    }
    return [rounds[number] for number in sorted(rounds)]


def _diagnosis(record: RunRecord) -> str | None:
    best = record.verification.best if record.verification else None
    return f"{best.mechanism.value}@{best.target_service}" if best else None


def _diagnosis_correct(diagnosis: str | None, expected: Mapping[str, Any]) -> bool | None:
    root, mechanism = expected.get("root_cause_service"), expected.get("mechanism")
    if not root or not mechanism:
        return None  # the scenario predeclares no answer to compare with
    return diagnosis == f"{mechanism}@{root}"


def _retrieval_mode(record: RunRecord) -> str:
    if not record.config.use_rag:
        return "off"
    if record.retrieval is not None:
        return record.retrieval.mode
    return record.config.retrieval or ""


def _retrieved_docs(record: RunRecord, trace: Sequence[TraceEvent]) -> list[str]:
    """Runbook sections retrieved for the run: RECAP's own searches, then the agents'."""
    ids = [doc.chunk.chunk_id for doc in record.retrieved_docs]
    for event in trace:
        if event.category == C.RETRIEVAL:
            ids += [str(r.get("chunk_id")) for r in event.payload.get("results") or []]
    return list(dict.fromkeys(ids))


def _agent_searches(record: RunRecord, trace: Sequence[TraceEvent]) -> int:
    if record.retrieval is not None:
        return record.retrieval.agent_searches
    return sum(1 for e in trace if e.category == C.RETRIEVAL and e.payload.get("stage") == "agent")


def _memory_hits(record: RunRecord) -> list[dict[str, Any]]:
    """Each remembered episode the run found, and RECAP's verdict on reusing it."""
    return [
        {
            "episode_id": hit.episode.episode_id,
            "partition": hit.episode.partition.value,
            "similarity": hit.similarity,
            "verified": hit.episode.verified,
            "safe_to_reuse": hit.safe_to_reuse,
            "reason": clean_text(hit.reason),
        }
        for hit in record.memory_hits
    ]


def _weights(record: RunRecord) -> str | None:
    server = record.provider_info.server if record.provider_info else None
    return _text_or_none((server or {}).get("weights"))


def _proposal_text(proposal: Any) -> str:
    params = ", ".join(f"{key}={value}" for key, value in sorted(proposal.params.items()))
    shown = f"{proposal.action} on {proposal.target}" + (f" ({params})" if params else "")
    return clean_text(f"{shown} [{proposal.source}]")


def _proposal_verdicts(events: Sequence[TraceEvent]) -> dict[str, str]:
    """Each proposal's policy verdict: the controller's first policy decision after it."""
    verdicts: dict[str, str] = {}
    waiting: str | None = None  # the proposal whose verdict comes next
    for event in events:
        if event.category == C.PROPOSAL and event.payload.get("proposal"):
            waiting = str((event.payload.get("proposal") or {}).get("proposal_id"))
        elif event.category == C.POLICY and event.payload.get("caller") == "controller" and waiting:
            verdicts[waiting] = _verdict_text(
                str(event.payload.get("verdict")), event.payload.get("rule_ids") or []
            )
            waiting = None
    return verdicts


def _record_verdict(record: RunRecord, index: int) -> str:
    """Without a trace: the run's policy decisions, by position (a fallback only: a refused
    execution adds a decision of its own, so positions can drift after one)."""
    decisions = record.safety_decisions
    if index < len(decisions):
        decision = decisions[index]
        return _verdict_text(decision.verdict.value, decision.rule_ids)
    return "no policy decision recorded"


def _verdict_text(verdict: str, rule_ids: Sequence[str]) -> str:
    """The verdict and the rule that decided it, e.g. APPROVAL_REQUIRED (R11-reversible-approval).

    The policy appends each rule just before checking it and stops at the deciding one
    (recap/safety/policy.py), so the deciding rule is the last one listed.
    """
    return f"{verdict} ({rule_ids[-1]})" if rule_ids else verdict


def _action_outcome(result: str | None) -> str:
    return {
        "recovered": "executed",
        "not_recovered": "executed",
        "error": "sent (the tool reported an error, or the run stopped while it was out)",
        "denied": "not executed",
        "rejected": "rejected by the approver, not executed",
        "expired": "approval expired, not executed",
        None: "no outcome recorded (the run stopped first)",
    }.get(result, str(result))


def _text_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _code(text: str) -> str:
    return f"`{text}`"


def _codes(values: Sequence[str]) -> str:
    return ", ".join(_code(v) for v in values) or "none"


def _cell(text: str) -> str:
    """Text for one markdown table cell or list item (no pipes, one line)."""
    return " ".join(str(text).replace("|", "/").split())


def created_at_of(records: Sequence[EvalRecord], default: datetime) -> datetime:
    """A batch's date: when its first run started (`default` for an empty batch)."""
    starts = [r.started_at for r in records if r.started_at is not None]
    return min(starts) if starts else default


class EventSource(Protocol):
    """Where a run's trace is read from (recap/telemetry/sinks.py, SqliteTraceSink)."""

    def list_events(
        self, run_id: str, after_seq: int = 0, limit: int = 5000
    ) -> list[TraceEvent]: ...


def run_events(source: EventSource, run_id: str, page: int = 5000) -> list[TraceEvent]:
    """Every trace event of one run, page by page (a long run has more than one page)."""
    events: list[TraceEvent] = []
    while True:
        batch = source.list_events(run_id, after_seq=events[-1].seq if events else 0, limit=page)
        events += batch
        if len(batch) < page:
            return events
