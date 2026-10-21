"""Export one run as a sanitized, shareable trace: the evidence that a run really happened.

    python scripts/export_run_trace.py RUN_ID evals/live/<name>
    python scripts/export_run_trace.py RUN_ID evals/live/<name> --replace   # overwrite
    RECAP_DATA_DIR=/some/data python scripts/export_run_trace.py RUN_ID evals/live/<name>

Writes these files into the output folder:

    trace.jsonl      every trace event of the run, in order
    run.json         the final checkpoint (RunRecord): state, findings, proposal, approvals,
                     actions, oracle verdict, usage, versions
    recording.jsonl  for a model-driven run: every HTTP exchange with the model server, as
                     recorded (the model's answers with the server's own token counts and
                     timings); the replay provider can re-run it offline
    README.md        a readable account built only from those files and the approval log:
                     who decided what, every model request, every tool call and policy
                     verdict, each approval and how it was given, the executed action, the
                     oracle's verdict, and the run's scope and caveats

Everything is cleaned first: known secrets and key formats are redacted, the data folder
becomes "data" and the home folder "~", and IDs that could link a recording to a provider
account (x-request-id, Groq's x_groq, an answer's own id) are dropped. Then every string of
every file is checked, decoded (not its JSON text) and URL-decoded too: if one still holds a
secret-looking environment value, a key-shaped string, the user's login name or a local
path, nothing at all is written. A one-word login name ("root", "user") is only looked for
where a path would hold it. Files are written to a staging folder and moved into place, so
a folder never mixes two exports. Load your provider env file first
(`set -a; . ~/.config/recap/groq.env; set +a`) so the value of its key is checked too.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import sys
from collections import Counter
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from recap.core.config import Settings, load_settings
from recap.core.models import ApprovalEvent, RunRecord, TraceEvent
from recap.harness.store import RunStore
from recap.providers.openai_compatible import KEPT_ANSWER_FIELDS
from recap.safety.approvals import ApprovalStore
from recap.telemetry.redact import MIN_ENV_SECRET_LENGTH, SECRET_ENV_NAME, Redactor
from recap.telemetry.sinks import SqliteTraceSink
from recap.telemetry.tracer import TraceCategory as C

# Keys dropped wherever they occur: IDs that could link a published recording to the
# provider account that made it (new recordings no longer keep them at all).
DROPPED_KEYS = frozenset({"x-request-id", "x_groq", "system_fingerprint", "request_id"})

# The last gate, run on every decoded string after cleaning: if one of these is still there,
# nothing is written. "Not preceded by a letter or digit" instead of \b, which also fails
# after a JSON escape such as \n.
_START = r"(?<![A-Za-z0-9])"
KEY_SHAPES = {
    "Groq key": re.compile(_START + r"gsk_[A-Za-z0-9]{20,}"),
    "OpenAI or Anthropic key": re.compile(
        _START + r"sk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{20,}"
    ),
    "AWS access key": re.compile(_START + r"(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])"),
    "GitHub token": re.compile(
        _START + r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    ),
    "Slack token": re.compile(_START + r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer token": re.compile(r"(?i)" + _START + r"bearer\s+[A-Za-z0-9._~+/-]{16,}"),
    "provider request ID": re.compile(r"(?i)" + _START + r"req_[0-9a-z]{20,}"),
    "provider answer ID": re.compile(_START + r"chatcmpl-[0-9A-Za-z-]{20,}"),
}
# Local paths, compared case-insensitively (macOS paths are).
LOCAL_PATHS = ("/users/", "/home/", "/var/folders/", "/private/tmp/", "\\users\\")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("run_id")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--replace", action="store_true", help="replace the folder if it already has files"
    )
    args = parser.parse_args()

    settings = load_settings()
    written = export_run(settings, args.run_id, args.out_dir, replace=args.replace)
    if written is None:
        print(f"no run {args.run_id} in {settings.paths.recap_db}")
        return 1
    print(f"wrote {', '.join(written)} to {args.out_dir}")
    return 0


def export_run(
    settings: Settings, run_id: str, out_dir: Path, *, replace: bool = False
) -> list[str] | None:
    """Write the sanitized files for one run; returns their names, or None if no such run."""
    record = RunStore(settings.paths.recap_db).get(run_id)
    if record is None:
        return None
    if out_dir.exists() and any(out_dir.iterdir()) and not replace:
        raise SystemExit(f"refusing to export: {out_dir} already has files (use --replace)")
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

    lines: list[dict[str, Any]] = []
    if recording is not None:
        raw = recording.read_text(encoding="utf-8").splitlines()
        lines = [clean_data(_without_answer_ids(json.loads(line))) for line in raw if line.strip()]
    data: dict[str, Any] = {
        "trace.jsonl": [clean_data(event.model_dump(mode="json")) for event in events],
        "run.json": clean_data(record.model_dump(mode="json")),
    }
    if recording is not None:
        data["recording.jsonl"] = lines
    data["README.md"] = clean(summary(record, events, decision_log, lines))
    for name, content in data.items():
        refuse_if_unsafe(name, content)

    files = {
        name: (
            content
            if name.endswith(".md")
            else json.dumps(content, indent=2) + "\n"
            if name.endswith(".json")
            else "".join(json.dumps(item) + "\n" for item in content)
        )
        for name, content in data.items()
    }
    # Stage, then move into place: the folder never mixes two exports or holds half of one.
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = out_dir.with_name(f".{out_dir.name}.exporting")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    for name, text in files.items():
        (staging / name).write_text(text, encoding="utf-8")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    staging.rename(out_dir)
    return list(files)


def _without_answer_ids(line: dict[str, Any]) -> dict[str, Any]:
    """An older recorded chat answer, cut to the fields the provider reads (no answer IDs)."""
    response = line.get("response")
    if str(line.get("path", "")).endswith("/chat/completions") and isinstance(response, dict):
        body = response.get("body")
        if isinstance(body, dict):
            kept = {k: v for k, v in body.items() if k in KEPT_ANSWER_FIELDS}
            return {**line, "response": {**response, "body": kept}}
    return line


def recording_file(settings: Settings, record: RunRecord) -> Path | None:
    """The run's model recording on disk, if it had one (stored relative to the repo root)."""
    shown = record.versions.get("recording")
    if not shown:
        return None
    path = Path(str(shown))
    if not path.is_absolute():
        path = settings.paths.root / path
    return path if path.is_file() else None


# --- the last gate -------------------------------------------------------------------------


def refuse_if_unsafe(name: str, content: Any) -> None:
    """Stop before anything is written if a file still holds a secret or a trace of this
    machine. Every string and dict key is checked as text, and URL-decoded too.

    The message names the file and the kind of finding, never the value itself.
    """
    secrets = [
        (var, value)
        for var, value in os.environ.items()
        if SECRET_ENV_NAME.search(var) and len(value) >= MIN_ENV_SECRET_LENGTH
    ]
    login = login_name_pattern()
    for text in _strings(content):
        for form in {text, unquote(text)}:
            for var, value in secrets:
                if value in form:
                    raise SystemExit(f"refusing to export: {name} contains the value of ${var}")
            for kind, shape in KEY_SHAPES.items():
                if shape.search(form):
                    raise SystemExit(f"refusing to export: {name} contains a {kind}")
            if login.search(form):
                raise SystemExit(f"refusing to export: {name} contains this user's login name")
            lowered = form.lower()
            for path in LOCAL_PATHS:
                if path in lowered:
                    raise SystemExit(
                        f"refusing to export: {name} contains a local path ({path}...)"
                    )


def _strings(value: Any) -> Iterator[str]:
    """Every string inside a JSON-like value, dict keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def login_name_pattern() -> re.Pattern[str]:
    """The user's login name as it could appear in exported text.

    A name of several parts ("jane.doe") is distinctive: it is looked for everywhere, with
    any separator or none ("jane-doe", "jane doe", "janedoe"). A one-word name ("root",
    "user", "bob") is also an ordinary word in RECAP's own text ("root cause", "role":
    "user"), so it is looked for only where a path would hold it: right after "/", "\\" or
    "-", and before one of those, a dot or the end ("/home/user/...", "pytest-of-user/").
    """
    parts = [re.escape(part) for part in re.split(r"[._\- ]", getpass.getuser()) if part]
    if not parts:
        return re.compile(r"(?!x)x")  # matches nothing
    if len(parts) > 1:
        return re.compile(
            r"(?<![A-Za-z0-9])" + r"[._\- ]?".join(parts) + r"(?![A-Za-z0-9])", re.IGNORECASE
        )
    return re.compile(r"(?<=[/\\-])" + parts[0] + r"(?=[/\\.-]|$)", re.IGNORECASE | re.MULTILINE)


# --- the README ----------------------------------------------------------------------------


def summary(
    record: RunRecord,
    events: list[TraceEvent],
    decision_log: list[ApprovalEvent],
    recording: list[dict[str, Any]],
) -> str:
    """The README.md: a readable account of the run, built only from its own records.

    Text written by the model (or anything outside RECAP) is escaped, so it cannot add
    lines or formatting that look like RECAP's own (for example a fake approval line).
    """
    calls = [e for e in events if e.category == C.LLM_CALL]
    lines = [f"# Run {record.run_id}: {record.scenario_id}", ""]
    lines += _overview(record, calls, recording)
    lines += _model_requests(calls)
    lines += _tools_and_policy(record, events)
    lines += _outcome(record, events, decision_log)
    lines += _usage(record, events, recording)
    lines += _scope(record, events, decision_log, recording)
    return "\n".join(lines) + "\n"


def _overview(
    record: RunRecord, calls: list[TraceEvent], recording: list[dict[str, Any]]
) -> list[str]:
    info = record.provider_info
    origin = info.origin if info else "?"
    if origin == "scripted":
        who = "the scripted policy (not an LLM)"
    elif origin == "replay":
        recorded = (info.replay_of or {}).get("recording_id") if info else "?"
        who = f"a replay of recording {_code(str(recorded))} (no model was called)"
    else:
        who = _code(record.provider)
    modes = Counter((c.payload.get("purpose"), c.payload.get("mode")) for c in calls)
    asked = "; ".join(
        f"{count} {_purpose(purpose, count)} in {_mode(mode)}"
        for (purpose, mode), count in sorted(modes.items(), key=lambda kv: str(kv[0]))
    )
    # The store's current decision (a later "no" can override an approval before it is used).
    last = record.approvals[-1] if record.approvals else None
    approval = f"{last.decision} by {_code(last.actor)}" if last else "no approval decision"
    lines = [
        f"- **Final state:** {record.state.value}: {_md(record.outcome_note)}",
        f"- **Agent decisions:** {who}, origin **{origin}**"
        + (f" ({asked})" if asked else "")
        + f". Routing, verification, the safety policy and the oracle are RECAP's own code; "
        f"the fix was {approval}.",
    ]
    server = (info.server if info else None) or _server_from(recording)
    if server:
        weights = server.get("weights")
        lines.append(
            f"- **Model server{' (of the recorded run)' if origin == 'replay' else ''}:** "
            f"Ollama {_md(str(server.get('ollama_version')))} · "
            f"{_code(info.model if info and info.model else '?')} "
            f"({_code(str(server.get('parameter_size')))}, "
            f"{_code(str(server.get('quantization')))})"
            + (f" · weights {_code(str(weights))}" if weights else " · weights digest not recorded")
        )
    lines += _free_plan_check(recording)
    lines += [
        f"- **Code:** {_code(str(record.versions.get('code_version', 'not recorded')))} · "
        f"prompts {_code(str(record.versions.get('prompt_version')))}",
        f"- **Environment:** {record.environment.kind if record.environment else '?'} "
        f"(simulated: {record.environment.is_simulated if record.environment else '?'})",
        f"- **Time:** {record.created_at.isoformat()} to {record.updated_at.isoformat()}",
    ]
    if recording:
        exchanges = recording[1:]
        chats = sum(1 for x in exchanges if _is_chat(x))
        original = str(record.versions.get("recording"))
        where = (
            f"the original is the committed {_code(original)}"
            if original.startswith("fixtures/")
            else f"run.json and trace.jsonl name the original, {_code(original)}, which is "
            "not published"
        )
        lines.append(
            f"- **Recording:** `recording.jsonl` in this folder: a header and {len(exchanges)} "
            f"HTTP exchanges with the model server ({chats} model requests with the server's "
            f"responses; metadata and answers are trimmed to the fields RECAP reads); {where}."
        )
    return [*lines, ""]


def _model_requests(calls: list[TraceEvent]) -> list[str]:
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
            f"| {number} | {_cell(event.agent or 'controller')} | {_cell(p.get('purpose'))} | "
            f"{_cell(p.get('mode'))} | {_code(str(model))} | {_cell(p.get('response_kind'))} | "
            f"{_count(p.get('input_tokens'))} | {_count(p.get('output_tokens'))} | "
            f"{(p.get('duration_ms') or 0) / 1000:.1f} | {'yes' if p.get('ok') else 'no'} |"
        )
        notes += [f"- request {number}: {_md(str(note))}" for note in p.get("notes") or []]
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
        call = f"{event.payload.get('tool')}({args})"
        lines.append(f"- {_code(str(event.agent))} → {_code(call)}")
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
            f"- {count} × {verdict}, decided by {_code(str(rule))} ({checked} rules checked)"
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
            f"{_code(str(best.target_service))} {_code(json.dumps(best.details))} "
            f"(cites {_code(cited)})"
        )
    for proposal in record.proposals:
        lines.append(
            f"- **Proposal ({proposal.source}):** {_code(proposal.action)} on "
            f"{_code(proposal.target)} {_code(json.dumps(proposal.params))}. Rationale "
            f"(planner text, untrusted): {_md(proposal.rationale)}"
        )
    for approval in record.approvals:
        own = [e for e in decision_log if e.proposal_id == approval.proposal_id]
        deciding = _deciding_event(own)
        wait = _approval_wait(own)
        lines.append(
            f"- **Approval:** {approval.decision} by {_code(approval.actor)} "
            f"({_md(approval_channel(approval.actor, deciding.detail if deciding else ''))}), "
            f"approval mode `{record.config.approval_mode}`, {approval.approval_id}"
            + (f", {wait:.1f} s after the request" if wait is not None else "")
        )
    if decision_log:
        lines.append("- **Approval decision log** (append-only):")
        lines += [
            f"  - {e.at.isoformat()} {e.kind} by {_code(e.actor)}"
            + (f": {_md(e.detail)}" if e.detail else "")
            for e in decision_log
        ]
    for event in (e for e in events if e.category == C.MITIGATION):
        p = event.payload
        effects = "; ".join(str(x) for x in p.get("side_effects") or []) or "no change reported"
        lines.append(
            f"- **Write:** {_code(str(p.get('tool')))} on {_code(str(p.get('target')))} "
            f"{_code(json.dumps(p.get('params')))}: ok {p.get('ok')}; {_md(effects)}; under "
            f"approval {_code(str(p.get('approval_id')))}"
        )
    for action in record.actions:
        lines.append(
            f"- **Action result:** {_code(action.action)} on {_code(action.target)} → "
            f"{_md(action.result)}" + (f" ({_md(action.note)})" if action.note else "")
        )
    if record.recovery:
        r = record.recovery
        lines.append(
            f"- **Oracle ({_code(r.oracle_name)}):** healthy before {r.healthy_before} → after "
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
        if _is_chat(x) and isinstance((x.get("response") or {}).get("body"), dict)
    )
    tokens = f"{u.actual_input_tokens or 0:,} input"
    replayed = record.provider_info is not None and record.provider_info.origin == "replay"
    if cached and not replayed:  # a replay called no model, so no cache either
        tokens += f" (of which {cached:,} came from the server's prompt cache)"
    cost = next((e.payload.get("cost") for e in events if e.category == C.RUN_SUMMARY), "")
    return [
        "## Usage",
        "",
        f"- {u.model_requests} model requests for {u.steps} agent decisions · {tokens} / "
        f"{u.actual_output_tokens or 0:,} output tokens, as the server reported them",
        f"- {tool_calls(u.tool_calls, events, approved)}",
        f"- Cost: {_md(str(cost))}",
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
        f"- One run of one simulated scenario ({_code(record.scenario_id)}): it shows that the "
        "path works end to end, not how often the model is right.",
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
        lines.append(
            "- Runbook retrieval (RAG) was on; chunks retrieved: "
            + ", ".join(_code(d) for d in docs)
        )
    for event in (e for e in events if e.category == C.MEMORY_WRITE):
        p = event.payload
        lines.append(
            f"- The verified run was saved to experience memory as "
            f"{_code(str(p.get('episode_id')))} (partition {_code(str(p.get('partition')))})."
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
                f"{', '.join(_code(a) for a in offered)}, a listed service, and evidence IDs "
                "the run had seen."
            )
        lines.append(
            "- The recording replays offline with the same RECAP code and prompts "
            f"({_code(str(record.versions.get('prompt_version')))}); after a prompt change a "
            "replay stops with ReplayMismatch instead of guessing."
        )
    waits = [
        wait
        for p in record.proposals
        if (wait := _approval_wait([e for e in decision_log if e.proposal_id == p.proposal_id]))
        is not None
    ]
    total = (record.updated_at - record.created_at).total_seconds()
    if waits:
        waited = "the approval" if len(waits) == 1 else f"{len(waits)} approvals"
        lines.append(
            f"- Wall time {total:.0f} s, of which {sum(waits):.0f} s waiting for {waited}."
        )
    return [*lines, ""]


def approval_channel(actor: str, note: str = "") -> str:
    """How an approval was given, in words (actor names come from recap/safety/approvals.py)."""
    if actor == "console-user":
        if note.startswith("rejected unread"):
            return "rejected automatically: no interactive terminal, nobody answered"
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


def _deciding_event(own: list[ApprovalEvent]) -> ApprovalEvent | None:
    """The log entry that set a proposal's current decision: the last approve, reject or
    revoke (a revocation is a "no" that overrode an approval before it was used)."""
    deciding = [e for e in own if e.kind in ("approved", "rejected", "revoked")]
    return deciding[-1] if deciding else None


def _approval_wait(own: list[ApprovalEvent]) -> float | None:
    """Seconds from a proposal's approval request to the entry that set its decision."""
    asked = [e.at for e in own if e.kind == "requested"]
    deciding = _deciding_event(own)
    if not asked or deciding is None:
        return None
    return (_aware(deciding.at) - _aware(asked[0])).total_seconds()


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
    return f"; {_code(target)} error rate {before['error_rate']} → {after['error_rate']}"


def _is_chat(exchange: dict[str, Any]) -> bool:
    """A model request (Ollama /api/chat or an OpenAI-style /chat/completions)."""
    return isinstance(exchange.get("request"), dict) and "messages" in exchange["request"]


def _free_plan_check(recording: list[dict[str, Any]]) -> list[str]:
    """For a hosted free plan: the daily request limit every recorded response carried."""
    limits, remaining = [], []
    for exchange in recording[1:]:
        headers = (exchange.get("response") or {}).get("headers") or {}
        if "x-ratelimit-limit-requests" in headers:
            limits.append(str(headers["x-ratelimit-limit-requests"]))
            remaining.append(str(headers.get("x-ratelimit-remaining-requests", "?")))
    if not limits:
        return []
    values = ", ".join(_code(v) for v in sorted(set(limits)))
    return [
        f"- **Free-plan check:** {len(limits)} recorded responses carried "
        f"`x-ratelimit-limit-requests` = {values} (requests per day; RECAP stops the provider "
        f"if it is not the free plan's); remaining requests went from {_md(remaining[0])} to "
        f"{_md(remaining[-1])}"
    ]


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
    names = {"investigate": "investigation request", "propose": "proposal request"}
    name = names.get(str(purpose), str(purpose))
    return name + ("s" if count != 1 else "")


def _mode(mode: Any) -> str:
    return {"tools": "tools mode", "json": "JSON-schema mode"}.get(str(mode), f"{mode} mode")


def _count(value: Any) -> str:
    return "–" if value is None else str(value)


# Inline markup that untrusted text could use to look like RECAP's own lines (bold labels,
# code, links, tables, strike-through). Block markup (headings, lists, quotes) needs a line
# start, and _md() removes every line break; "<" is escaped, so no HTML such as <br> either.
_MARKDOWN = re.compile(r"([\\`*_\[\]|~])")


def _md(text: str) -> str:
    """Untrusted text as inert markdown on one line: no line breaks, HTML or inline markup."""
    flat = " ".join(str(text).split()).replace("&", "&amp;").replace("<", "&lt;")
    return _MARKDOWN.sub(r"\\\1", flat)


def _code(text: str) -> str:
    """Untrusted text as an inline code span (backticks and newlines removed)."""
    return "`" + " ".join(str(text).replace("`", "'").split()) + "`"


def _cell(value: Any) -> str:
    """A table cell: inert text without pipes."""
    return _md(str(value)).replace("|", "\\|")


if __name__ == "__main__":
    raise SystemExit(main())
