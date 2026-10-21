"""Export one run as a sanitized, shareable trace: the evidence that a run really happened.

    python scripts/export_run_trace.py RUN_ID evals/live/<name>
    RECAP_DATA_DIR=/some/data python scripts/export_run_trace.py RUN_ID evals/live/<name>

Writes these files into the output folder:

    trace.jsonl      every trace event of the run, in order
    run.json         the final checkpoint (RunRecord): state, findings, proposal, approvals,
                     actions, oracle verdict, usage, versions
    recording.jsonl  for a model-driven run: every HTTP exchange with the model server, as
                     recorded (the model's raw answers, with the server's own token counts
                     and timings); the replay provider can re-run it offline
    README.md        a readable account built only from those files and the approval log:
                     who decided what, every model request, every tool call and policy
                     verdict, the approval and how it was given, the executed action, the
                     oracle's verdict, and the run's scope and caveats

Everything is cleaned before it is written: known secrets and key formats are redacted, the
data folder becomes "data" and the home folder "~", and response headers that could identify
an account (x-request-id) are dropped. Then nothing at all is written if any file still holds
a secret-looking environment value, a key-shaped string, the user's login name or a local
path. Load your provider env file first (`set -a; . ~/.config/recap/groq.env; set +a`) so the
value of its key is checked too.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from recap.core.config import Settings, load_settings
from recap.core.models import ApprovalEvent, RunRecord, TraceEvent
from recap.harness.store import RunStore
from recap.safety.approvals import ApprovalStore
from recap.telemetry.redact import MIN_ENV_SECRET_LENGTH, SECRET_ENV_NAME, Redactor
from recap.telemetry.sinks import SqliteTraceSink
from recap.telemetry.tracer import TraceCategory as C

# Keys dropped wherever they occur: a provider's request ID could link a published
# recording to the account that made it (new recordings no longer keep it at all).
DROPPED_KEYS = frozenset({"x-request-id"})

# A last gate after cleaning: if any of these is still in a file, nothing is written.
KEY_SHAPES = {
    "Groq key": re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),
    "OpenAI or Anthropic key": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}"),
}
LOCAL_PATHS = ("/Users/", "/home/", "/var/folders/", "/private/tmp/")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("run_id")
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()

    settings = load_settings()
    written = export_run(settings, args.run_id, args.out_dir)
    if written is None:
        print(f"no run {args.run_id} in {settings.paths.recap_db}")
        return 1
    print(f"wrote {', '.join(written)} to {args.out_dir}")
    return 0


def export_run(settings: Settings, run_id: str, out_dir: Path) -> list[str] | None:
    """Write the sanitized files for one run; returns their names, or None if no such run."""
    record = RunStore(settings.paths.recap_db).get(run_id)
    if record is None:
        return None
    events = SqliteTraceSink(settings.paths.recap_db).list_events(run_id)
    approvals = ApprovalStore(settings.paths.recap_db)
    decision_log = [e for p in record.proposals for e in approvals.events(p.proposal_id)]

    recording = recording_file(settings, record)
    if recording is None and record.versions.get("recording"):
        # Without it the export could not show what the model was actually sent.
        raise SystemExit(
            f"refusing to export: the run's model recording {record.versions['recording']} "
            "is missing"
        )

    redact = Redactor()
    data_dir, home = str(settings.paths.data_dir), str(Path.home())

    def clean(text: str) -> str:
        return redact.redact_text(text).replace(data_dir, "data").replace(home, "~")

    def clean_data(value: Any) -> Any:
        # Clean every string IN the data, then serialize: cleaning serialized JSON could cut
        # through an escaped quote and leave text that no longer parses.
        if isinstance(value, str):
            return clean(value)
        if isinstance(value, dict):
            return {k: clean_data(v) for k, v in value.items() if k.lower() not in DROPPED_KEYS}
        if isinstance(value, list):
            return [clean_data(item) for item in value]
        return value

    lines = []
    if recording is not None:
        raw = recording.read_text(encoding="utf-8").splitlines()
        lines = [clean_data(json.loads(line)) for line in raw if line.strip()]
    files = {
        "trace.jsonl": "".join(
            json.dumps(clean_data(event.model_dump(mode="json"))) + "\n" for event in events
        ),
        "run.json": json.dumps(clean_data(record.model_dump(mode="json")), indent=2) + "\n",
    }
    if recording is not None:
        files["recording.jsonl"] = "".join(json.dumps(line) + "\n" for line in lines)
    files["README.md"] = clean(summary(record, events, decision_log, lines))
    for name, text in files.items():
        refuse_if_unsafe(name, text)

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8")
    return list(files)


def recording_file(settings: Settings, record: RunRecord) -> Path | None:
    """The run's model recording on disk, if it had one (stored relative to the repo root)."""
    shown = record.versions.get("recording")
    if not shown:
        return None
    path = Path(str(shown))
    if not path.is_absolute():
        path = settings.paths.root / path
    return path if path.is_file() else None


def refuse_if_unsafe(name: str, text: str) -> None:
    """Stop before writing anything if a file still holds a secret or a trace of this machine.

    The message names the file and the kind of finding, never the value itself.
    """
    for var, value in os.environ.items():
        if SECRET_ENV_NAME.search(var) and len(value) >= MIN_ENV_SECRET_LENGTH and value in text:
            raise SystemExit(f"refusing to export: {name} contains the value of ${var}")
    for kind, shape in KEY_SHAPES.items():
        if shape.search(text):
            raise SystemExit(f"refusing to export: {name} contains a {kind}")
    if login_name_pattern().search(text):
        raise SystemExit(f"refusing to export: {name} contains this user's login name")
    for path in LOCAL_PATHS:
        if path in text:
            raise SystemExit(f"refusing to export: {name} contains a local path ({path}...)")


def login_name_pattern() -> re.Pattern[str]:
    """The user's login name, also as it appears in mangled paths ("jane.doe", "jane-doe")."""
    user = getpass.getuser()
    parts = [re.escape(part) for part in re.split(r"[._-]", user) if part]
    if len(user) < 4 or not parts:  # too short to search for without false alarms
        return re.compile(r"(?!x)x")  # matches nothing
    return re.compile(r"\b" + r"[._-]".join(parts) + r"\b", re.IGNORECASE)


# --- the README ----------------------------------------------------------------------------


def summary(
    record: RunRecord,
    events: list[TraceEvent],
    decision_log: list[ApprovalEvent],
    recording: list[dict[str, Any]],
) -> str:
    """The README.md: a readable account of the run, built only from its own records."""
    calls = [e for e in events if e.category == C.LLM_CALL]
    lines = [f"# Run {record.run_id}: {record.scenario_id}", ""]
    lines += _overview(record, calls, decision_log, recording)
    lines += _model_requests(calls, recording)
    lines += _tools_and_policy(record, events)
    lines += _outcome(record, events, decision_log)
    lines += _usage(record, events, recording)
    lines += _scope(record, events, decision_log, recording)
    return "\n".join(lines) + "\n"


def _overview(
    record: RunRecord,
    calls: list[TraceEvent],
    decision_log: list[ApprovalEvent],
    recording: list[dict[str, Any]],
) -> list[str]:
    info = record.provider_info
    origin = info.origin if info else "?"
    if origin == "scripted":
        who = "the scripted policy (not an LLM)"
    elif origin == "replay":
        who = (
            f"a replay of recording `{(info.replay_of or {}).get('recording_id') if info else '?'}`"
        )
        who += " (no model was called)"
    else:
        who = f"`{record.provider}`"
    modes = Counter((c.payload.get("purpose"), c.payload.get("mode")) for c in calls)
    asked = "; ".join(
        f"{count} {_purpose(purpose, count)} in {_mode(mode)}"
        for (purpose, mode), count in sorted(modes.items(), key=lambda kv: str(kv[0]))
    )
    approved = [e for e in decision_log if e.kind in ("approved", "rejected")]
    approval = (
        f"{approved[-1].kind} by `{approved[-1].actor}`" if approved else "no approval decision"
    )
    lines = [
        f"- **Final state:** {record.state.value}: {record.outcome_note}",
        f"- **Agent decisions:** {who}, origin **{origin}**"
        + (f" ({asked})" if asked else "")
        + f". Routing, verification, the safety policy and the oracle are RECAP's own code; "
        f"the fix was {approval}.",
    ]
    server = (info.server if info else None) or _server_from(recording)
    if server:
        weights = server.get("weights")
        lines.append(
            f"- **Model server:** Ollama {server.get('ollama_version')} · "
            f"`{info.model if info else '?'}` ({server.get('parameter_size')}, "
            f"{server.get('quantization')})"
            + (f" · weights `{weights}`" if weights else " · weights digest not recorded")
        )
    lines += [
        f"- **Code:** `{record.versions.get('code_version', 'not recorded')}` · prompts "
        f"`{record.versions.get('prompt_version')}`",
        f"- **Environment:** {record.environment.kind if record.environment else '?'} "
        f"(simulated: {record.environment.is_simulated if record.environment else '?'})",
        f"- **Time:** {record.created_at.isoformat()} to {record.updated_at.isoformat()}",
    ]
    if recording:
        exchanges = recording[1:]
        chats = sum(1 for x in exchanges if x.get("path") == "/api/chat")
        lines.append(
            f"- **Recording:** `recording.jsonl` in this folder: a header and {len(exchanges)} "
            f"HTTP exchanges with the model server ({chats} model requests with the raw "
            "answers; metadata answers are trimmed to the fields RECAP reads). run.json and "
            "trace.jsonl name the original under `data/recordings/`, which is not published."
        )
    return [*lines, ""]


def _model_requests(calls: list[TraceEvent], recording: list[dict[str, Any]]) -> list[str]:
    if not calls:
        return []
    lines = [
        "## Model requests",
        "",
        "Token counts are the server's own; model text is untrusted.",
        "",
        "| # | agent | purpose | mode | model (as reported) | answer | in | out | seconds | ok |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    notes = []
    for number, event in enumerate(calls, 1):
        p = event.payload
        model = p.get("model_reported") or p.get("model")
        lines.append(
            f"| {number} | {event.agent or 'controller'} | {p.get('purpose')} | {p.get('mode')} "
            f"| `{model}` | {p.get('response_kind')} | {p.get('input_tokens')} | "
            f"{p.get('output_tokens')} | {(p.get('duration_ms') or 0) / 1000:.1f} | "
            f"{'yes' if p.get('ok') else 'no'} |"
        )
        notes += [f"- request {number}: {note}" for note in p.get("notes") or []]
    return [*lines, "", *notes, *([""] if notes else [])]


def _tools_and_policy(record: RunRecord, events: list[TraceEvent]) -> list[str]:
    info = record.provider_info
    chooser = {
        "scripted": "chosen by the scripted policy",
        "replay": "replayed from a recorded model run",
    }.get(info.origin if info else "", "chosen by the model")
    lines = ["## Read-only tool calls", "", f"{chooser.capitalize()}, run by the gateway:", ""]
    for event in (e for e in events if e.category == C.TOOL_CALL):
        args = ", ".join(f"{k}={v}" for k, v in (event.payload.get("args") or {}).items())
        lines.append(f"- `{event.agent}` → `{event.payload.get('tool')}({args})`")
    # The policy evaluates its rules in order; the last rule listed is the one that decided.
    verdicts = Counter(
        (
            e.payload.get("verdict"),
            (e.payload.get("rule_ids") or ["?"])[-1],
            len(e.payload.get("rule_ids") or []),
        )
        for e in events
        if e.category == C.POLICY
    )
    if verdicts:
        lines += ["", "Policy decisions (every call is judged before it is sent):", ""]
        lines += [
            f"- {count} × {verdict}, decided by `{rule}` ({checked} rules checked)"
            for (verdict, rule, checked), count in verdicts.items()
        ]
    return [*lines, ""]


def _outcome(
    record: RunRecord, events: list[TraceEvent], decision_log: list[ApprovalEvent]
) -> list[str]:
    lines = ["## Diagnosis, proposal, approval, action, oracle", ""]
    if record.verification and record.verification.best:
        best = record.verification.best
        cited = ", ".join(best.supporting_evidence_ids)
        lines.append(
            f"- **Verifier:** {record.verification.status}: {best.mechanism.value} on "
            f"`{best.target_service}` {best.details} (cites {cited})"
        )
    for proposal in record.proposals:
        lines.append(
            f"- **Proposal ({proposal.source}):** `{proposal.action}` on `{proposal.target}` "
            f"{proposal.params}: {proposal.rationale}"
        )
    for approval in record.approvals:
        lines.append(
            f"- **Approval:** {approval.decision} by `{approval.actor}` "
            f"({approval_channel(approval.actor, _note(decision_log, approval.decision))}), "
            f"approval mode `{record.config.approval_mode}`, {approval.approval_id}"
        )
    if decision_log:
        lines.append("- **Approval decision log** (append-only):")
        lines += [
            f"  - {e.at.isoformat()} {e.kind} by `{e.actor}`"
            + (f": {e.detail}" if e.detail else "")
            for e in decision_log
        ]
        wait = _approval_wait(decision_log)
        if wait is not None:
            lines.append(f"  - from request to decision: {wait:.1f} s")
    for event in (e for e in events if e.category == C.MITIGATION):
        p = event.payload
        effects = "; ".join(p.get("side_effects") or []) or "no change reported"
        lines.append(
            f"- **Write:** `{p.get('tool')}` on `{p.get('target')}` {p.get('params')}: "
            f"ok {p.get('ok')}; {effects}; under approval `{p.get('approval_id')}`"
        )
    for action in record.actions:
        lines.append(
            f"- **Action result:** `{action.action}` on `{action.target}` → {action.result}"
            + (f" ({action.note})" if action.note else "")
        )
    if record.recovery:
        r = record.recovery
        lines.append(
            f"- **Oracle ({r.oracle_name}):** healthy before {r.healthy_before} → after "
            f"{r.healthy_after}; verified {r.verified}{_error_rates(record)}"
        )
    return [*lines, ""]


def _usage(
    record: RunRecord, events: list[TraceEvent], recording: list[dict[str, Any]]
) -> list[str]:
    u = record.usage
    approved = {a.approval_id for a in record.approvals if a.consumed_at is not None}
    cached = sum(
        int((x.get("response") or {}).get("body", {}).get("prompt_eval_cached_count") or 0)
        for x in recording[1:]
        if x.get("path") == "/api/chat" and isinstance((x.get("response") or {}).get("body"), dict)
    )
    tokens = f"{u.actual_input_tokens or 0:,} input"
    if cached:
        tokens += f" (of which {cached:,} came from the server's prompt cache)"
    cost = next((e.payload.get("cost") for e in events if e.category == C.RUN_SUMMARY), "")
    return [
        "## Usage",
        "",
        f"- {u.model_requests} model requests for {u.steps} agent decisions · {tokens} / "
        f"{u.actual_output_tokens or 0:,} output tokens, as the server reported them",
        f"- {tool_calls(u.tool_calls, events, approved)}",
        f"- Cost: {cost}",
        "",
    ]


def _scope(
    record: RunRecord,
    events: list[TraceEvent],
    decision_log: list[ApprovalEvent],
    recording: list[dict[str, Any]],
) -> list[str]:
    """What this run does and does not show."""
    config = record.config
    lines = [
        "## Scope and caveats",
        "",
        f"- One run of one simulated scenario (`{record.scenario_id}`): it shows that the path "
        "works end to end, not how often the model is right.",
        "- Memory: " + ("read" if config.use_memory else "not read (memory off for this run)"),
    ]
    docs = sorted(
        {
            str(item.get("chunk_id") or item.get("id"))
            for e in events
            if e.category == C.RETRIEVAL
            for item in (e.payload.get("results") or [])
            if isinstance(item, dict) and (item.get("chunk_id") or item.get("id"))
        }
    )
    if docs:
        lines.append(f"- Runbook retrieval (RAG) was on; chunks retrieved: {', '.join(docs)}")
    for event in (e for e in events if e.category == C.MEMORY_WRITE):
        p = event.payload
        lines.append(
            f"- The verified run was saved to experience memory as `{p.get('episode_id')}` "
            f"(partition `{p.get('partition')}`)."
        )
    if recording:
        settings = recording[0].get("settings") or {}
        lines.append(
            f"- Sampling: temperature {settings.get('temperature')}, seed {settings.get('seed')}, "
            f"thinking {'on' if settings.get('think') else 'off'}, at most "
            f"{settings.get('max_output_tokens')} output tokens per answer, context window "
            f"{settings.get('context_tokens')} tokens."
        )
        offered = _proposal_actions(recording)
        if offered:
            lines.append(
                "- The proposal answer had to follow a JSON schema: an action from "
                f"{', '.join(f'`{a}`' for a in offered)}, a listed service, and evidence IDs "
                "the run had seen."
            )
        lines.append(
            "- The recording replays offline with the same RECAP code and prompts "
            f"(`{record.versions.get('prompt_version')}`); after a prompt change a replay stops "
            "with ReplayMismatch instead of guessing."
        )
    wait = _approval_wait(decision_log)
    total = (record.updated_at - record.created_at).total_seconds()
    if wait is not None:
        lines.append(f"- Wall time {total:.0f} s, of which {wait:.0f} s waiting for the approval.")
    return [*lines, ""]


def approval_channel(actor: str, note: str = "") -> str:
    """How an approval was given, in words (actor names come from recap/safety/approvals.py)."""
    if actor == "console-user":
        if note:
            return f"answered at the console prompt: {note}"
        return (
            "answered at the console prompt; this RECAP version did not record whether it was "
            "an interactive terminal"
        )
    if actor.startswith("auto-approver"):
        return "the simulation auto-approver, not a person"
    if actor.startswith("operator:"):
        return "a signed-in operator, through the API or the dashboard"
    return "another approval channel"


def tool_calls(count: int, events: list[TraceEvent], approved: set[str]) -> str:
    """The run's tool-call count, split into reads and writes when the trace accounts for all.

    A write counts as approved only if it ran under an approval that was actually used.
    """
    reads = sum(1 for e in events if e.category == C.TOOL_CALL)
    writes = [e for e in events if e.category == C.MITIGATION]
    if reads + len(writes) != count:
        return f"{count} tool calls"
    ok = sum(1 for e in writes if e.payload.get("approval_id") in approved)
    parts = [f"{reads} reads"]
    if ok:
        parts.append(f"{ok} approved write{'s' * (ok != 1)}")
    if len(writes) - ok:
        parts.append(f"{len(writes) - ok} write(s) without a used approval")
    return f"{count} tool calls ({', '.join(parts)})"


def _note(decision_log: list[ApprovalEvent], kind: str) -> str:
    notes = [e.detail for e in decision_log if e.kind == kind]
    return notes[-1] if notes else ""


def _approval_wait(decision_log: list[ApprovalEvent]) -> float | None:
    """Seconds from the approval request to the human's (or approver's) decision."""
    asked = [e.at for e in decision_log if e.kind == "requested"]
    decided = [e.at for e in decision_log if e.kind in ("approved", "rejected")]
    if not asked or not decided:
        return None
    return (_aware(decided[0]) - _aware(asked[0])).total_seconds()


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.astimezone()


def _error_rates(record: RunRecord) -> str:
    """The target service's error rate before and after, from the oracle's own observations."""
    r = record.recovery
    target = record.proposals[-1].target if record.proposals else None
    if r is None or target is None:
        return ""
    signals = r.observed_signals or {}
    before = (signals.get("before") or {}).get(target) or {}
    after = (signals.get("after") or {}).get(target) or {}
    if "error_rate" not in before or "error_rate" not in after:
        return ""
    return f"; `{target}` error rate {before['error_rate']} → {after['error_rate']}"


def _server_from(recording: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Ollama's version and the model's size from a recording's metadata answers (for runs
    made before RECAP stored these facts in ProviderInfo.server)."""
    bodies = {
        x.get("path"): (x.get("response") or {}).get("body")
        for x in recording[1:]
        if x.get("path") in ("/api/version", "/api/show")
    }
    version, show = bodies.get("/api/version"), bodies.get("/api/show")
    if not isinstance(version, dict) or not isinstance(show, dict):
        return None
    details = show.get("details") or {}
    return {
        "ollama_version": version.get("version"),
        "parameter_size": details.get("parameter_size"),
        "quantization": details.get("quantization_level"),
    }


def _proposal_actions(recording: list[dict[str, Any]]) -> list[str]:
    """The actions the proposal schema offered, from the recorded proposal request."""
    for exchange in recording[1:]:
        schema = (exchange.get("request") or {}).get("format")
        if isinstance(schema, dict):
            action = (schema.get("properties") or {}).get("action") or {}
            if action.get("enum"):
                return [str(a) for a in action["enum"]]
    return []


def _purpose(purpose: Any, count: int) -> str:
    names = {"investigate": "investigation step", "propose": "proposal"}
    name = names.get(str(purpose), str(purpose))
    return name + ("s" if count != 1 else "")


def _mode(mode: Any) -> str:
    return {"tools": "tools mode", "json": "JSON-schema mode"}.get(str(mode), f"{mode} mode")


if __name__ == "__main__":
    raise SystemExit(main())
