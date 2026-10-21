"""Export one run as a sanitized, shareable trace: the evidence that a run really happened.

    python scripts/export_run_trace.py RUN_ID evals/live/<name>
    RECAP_DATA_DIR=/some/data python scripts/export_run_trace.py RUN_ID evals/live/<name>

Writes three files into the output folder:

    trace.jsonl   every trace event of the run, in order (the tracer already redacted
                  secrets; here the user's home folder also becomes "~")
    run.json      the final checkpoint (RunRecord): state, findings, proposal, approvals,
                  actions, oracle verdict, usage, versions
    README.md     a readable summary: who decided (provider, model, origin), every model
                  request with its exact token counts, every tool call, the approval, the
                  executed action and the oracle's verdict

It refuses to write anything that still contains the value of an environment variable
that looks like a secret (KEY, SECRET, TOKEN, PASSWORD in its name).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from recap.core.config import load_settings
from recap.core.models import RunRecord, TraceEvent
from recap.harness.store import RunStore
from recap.telemetry.redact import MIN_ENV_SECRET_LENGTH, SECRET_ENV_NAME, Redactor
from recap.telemetry.sinks import SqliteTraceSink
from recap.telemetry.tracer import TraceCategory as C


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("run_id")
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()

    settings = load_settings()
    record = RunStore(settings.paths.recap_db).get(args.run_id)
    if record is None:
        print(f"no run {args.run_id} in {settings.paths.recap_db}")
        return 1
    events = SqliteTraceSink(settings.paths.recap_db).list_events(args.run_id)

    redact = Redactor()
    home = str(Path.home())

    def clean(text: str) -> str:
        return redact.redact_text(text).replace(home, "~")

    trace = "".join(clean(event.model_dump_json()) + "\n" for event in events)
    run = clean(record.model_dump_json(indent=2))
    readme = clean(summary(record, events))
    for text in (trace, run, readme):
        refuse_if_secret(text)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "trace.jsonl").write_text(trace, encoding="utf-8")
    (args.out_dir / "run.json").write_text(run + "\n", encoding="utf-8")
    (args.out_dir / "README.md").write_text(readme, encoding="utf-8")
    print(f"wrote {len(events)} events to {args.out_dir}")
    return 0


def refuse_if_secret(text: str) -> None:
    """Stop if any secret-looking environment value survived redaction."""
    for name, value in os.environ.items():
        if SECRET_ENV_NAME.search(name) and len(value) >= MIN_ENV_SECRET_LENGTH and value in text:
            raise SystemExit(f"refusing to export: the value of ${name} appears in the output")


def summary(record: RunRecord, events: list[TraceEvent]) -> str:
    """The README.md: a readable account of the run, built only from its own records."""
    info = record.provider_info
    lines = [
        f"# Run {record.run_id}: {record.scenario_id}",
        "",
        f"- **Final state:** {record.state.value} — {record.outcome_note}",
        f"- **Decisions by:** `{record.provider}` (origin **{info.origin if info else '?'}**, "
        f"model `{info.model if info else '?'}`, {info.mode if info else '?'} mode, "
        f"{record.versions.get('prompt_version')})",
        f"- **Environment:** {record.environment.kind if record.environment else '?'} "
        f"(simulated: {record.environment.is_simulated if record.environment else '?'})",
        f"- **Started:** {record.created_at.isoformat()}",
        f"- **Recording:** `{record.versions.get('recording')}`",
        "",
        "## Model requests",
        "",
        "Exact token counts as reported by the model server; model text is untrusted.",
        "",
        "| # | agent | purpose | answer | in | out | seconds | ok |",
        "|---|---|---|---|---|---|---|---|",
    ]
    calls = [e for e in events if e.category == C.LLM_CALL]
    for number, event in enumerate(calls, 1):
        p = event.payload
        lines.append(
            f"| {number} | {event.agent or 'controller'} | {p.get('purpose')} | "
            f"{p.get('response_kind')} | {p.get('input_tokens')} | {p.get('output_tokens')} | "
            f"{(p.get('duration_ms') or 0) / 1000:.1f} | {'yes' if p.get('ok') else 'no'} |"
        )
    lines += ["", "## Tool calls (chosen by the model, run by the gateway)", ""]
    for event in events:
        if event.category == C.TOOL_CALL:
            args = ", ".join(f"{k}={v}" for k, v in (event.payload.get("args") or {}).items())
            lines.append(f"- `{event.agent}` → `{event.payload.get('tool')}({args})`")
    lines += ["", "## Diagnosis, proposal, approval, action, oracle", ""]
    if record.verification and record.verification.best:
        best = record.verification.best
        cited = ", ".join(best.supporting_evidence_ids)
        lines.append(
            f"- **Verifier:** {record.verification.status} — {best.mechanism.value} on "
            f"`{best.target_service}` {best.details} (cites {cited})"
        )
    for proposal in record.proposals:
        lines.append(
            f"- **Proposal ({proposal.source}):** `{proposal.action}` on `{proposal.target}` "
            f"{proposal.params} — {proposal.rationale}"
        )
    for approval in record.approvals:
        lines.append(
            f"- **Approval:** {approval.decision} by `{approval.actor}` ({approval.approval_id})"
        )
    for action in record.actions:
        lines.append(
            f"- **Action:** `{action.action}` on `{action.target}` → {action.result} {action.note}"
        )
    if record.recovery:
        r = record.recovery
        lines.append(
            f"- **Oracle ({r.oracle_name}):** healthy before {r.healthy_before} → after "
            f"{r.healthy_after}; verified {r.verified}"
        )
    u = record.usage
    cost = next((e.payload.get("cost") for e in events if e.category == C.RUN_SUMMARY), "")
    lines += [
        "",
        "## Usage",
        "",
        f"- {u.model_requests} model requests · {u.actual_input_tokens} input / "
        f"{u.actual_output_tokens} output tokens (reported) · {u.tool_calls} tool calls · "
        f"{u.steps} decisions",
        f"- Cost: {cost}",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
