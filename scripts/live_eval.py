"""Run a planned set of incidents with a REAL model and summarize each one, truthfully.

    python scripts/live_eval.py evals/plans/m3-real-models.yaml --out /tmp/recap-live
    python scripts/live_eval.py PLAN --out DIR --only unfamiliar      # one run of the plan

A plan (YAML) names the provider (ollama, or groq on its free plan: never a paid API) and
the runs, in order, in ONE data folder, so a later run can find an earlier run's episode in
memory. Per run it may force the routing strategy, choose retrieval (bm25 or hybrid), add
knowledge overlays (a red-team poisoned runbook) and plant a false memory first (both given
repo-relative). Every run is approved by the SIMULATION auto-approver; the human-approved
deliverable is a separate `scripts/demo.py --approve ask` run. The whole plan is checked
before its first run (scenarios, folders and files exist), so a typo never stops a long
real-model plan halfway; the repository's own data/ folder is refused under any spelling.

The summary (summary.json and summary.md in --out) says only what the run records say:
the state, the model, its requests and the tokens the server reported, each agent's tool
calls in order, the findings' outcomes, the disagreements, the retrieval mode and searches,
the memory verdicts, the fix and the oracle's verdict. Search queries and claims are model
text: summary.md shows them as quoted data.

Groq needs RECAP_ALLOW_LIVE_FREE_INFERENCE=1 and the owner's free-plan confirmation in the
environment (see docs/provider-options.md); the free-tier guard checks every request.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Literal

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from pydantic import Field, ValidationError

from recap.core.config import REPO_ROOT, Settings, load_settings
from recap.core.models import (
    Contract,
    ExperienceEpisode,
    MemoryPartition,
    RunConfig,
    RunRecord,
    TraceEvent,
)
from recap.environments.registry import create_environment
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from scripts.redteam_setup import is_repository_data
from scripts.redteam_setup import main as plant

MODEL_ORIGINS = ("local_llm", "free_hosted_llm")  # what a live evaluation may use


class PlannedRun(Contract):
    id: str
    scenario: str
    strategy: Literal["adaptive", "single", "parallel"] = "adaptive"
    retrieval: Literal["bm25", "hybrid"] | None = None  # None: configs/retrieval.yaml
    use_memory: bool = True
    overlays: list[str] = Field(default_factory=list)  # repo-relative knowledge folders
    # A repo-relative episode YAML planted before this run (red team).
    false_memory: str | None = None
    read_partitions: list[MemoryPartition] | None = None


class Plan(Contract):
    provider: str
    runs: list[PlannedRun]


def load_plan(path: Path) -> Plan:
    try:
        plan = Plan.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ValidationError as exc:  # e.g. a mistyped strategy: refused before anything runs
        raise SystemExit(f"{path}: {exc}") from None
    ids = [run.id for run in plan.runs]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"{path}: run ids must be unique")
    return plan


def check_plan(plan: Plan, settings: Settings) -> list[str]:
    """What would stop the plan halfway: an unknown scenario, a knowledge folder that does
    not exist, a false memory that is missing or not an episode. Checked before the first
    run, because a real-model plan takes hours."""
    environment = create_environment(settings.environment, "describe")
    known = {scenario.id for scenario in environment.list_scenarios()}
    problems: list[str] = []
    for run in plan.runs:
        if run.scenario not in known:
            problems.append(f"{run.id}: unknown scenario {run.scenario!r}")
        problems += [
            f"{run.id}: knowledge folder {folder} not found"
            for folder in run.overlays
            if not (REPO_ROOT / folder).is_dir()
        ]
        if run.false_memory:
            try:
                raw = yaml.safe_load((REPO_ROOT / run.false_memory).read_text(encoding="utf-8"))
                ExperienceEpisode.model_validate(raw)
            except (OSError, yaml.YAMLError, ValidationError) as exc:
                reason = type(exc).__name__
                problems.append(f"{run.id}: false memory {run.false_memory} unusable ({reason})")
    return problems


def settings_for(base: Settings, run: PlannedRun) -> Settings:
    """The run's settings: the shared data folder, plus this run's knowledge overlays
    (resolved, as RECAP_KNOWLEDGE_OVERLAYS is, so a run records a folder outside the
    repository by its own name)."""
    overlays = [(REPO_ROOT / folder).resolve() for folder in run.overlays]
    paths = base.paths.model_copy(update={"knowledge_overlays": overlays})
    return base.model_copy(update={"paths": paths})


async def run_one(base: Settings, plan: Plan, run: PlannedRun) -> dict[str, Any]:
    settings = settings_for(base, run)
    if run.false_memory:
        memory = str(REPO_ROOT / run.false_memory)
        code = plant(["--data-dir", str(settings.paths.data_dir), "--false-memory", memory])
        if code != 0:
            raise SystemExit(f"{run.id}: could not plant {run.false_memory}")
    runtime = Runtime(settings)
    config = RunConfig(
        provider=plan.provider,
        strategy=run.strategy,
        retrieval=run.retrieval,
        use_memory=run.use_memory,
        memory_read_partitions=run.read_partitions,
        approval_mode="auto",
        label=f"live-eval:{run.id}",
    )
    started = time.perf_counter()
    record = await runtime.run_incident(run.scenario, config)
    seconds = round(time.perf_counter() - started, 1)
    events = runtime.events.list_events(record.run_id)
    return summarize(run, record, events, seconds, settings)


def summarize(
    run: PlannedRun, record: RunRecord, events: list[TraceEvent], seconds: float, settings: Settings
) -> dict[str, Any]:
    """What happened, taken from the run record and its trace only."""
    info = record.provider_info
    usage = record.usage
    calls: dict[str, list[str]] = {}
    for e in events:
        if e.category == C.TOOL_CALL:
            args = ",".join(f"{k}={v}" for k, v in sorted(e.payload.get("args", {}).items()))
            calls.setdefault(str(e.payload.get("caller")), []).append(
                f"{e.payload['tool']}({args})"
            )
    finishes = [e.payload for e in events if e.category == C.AGENT_FINISH]
    searches = [
        {
            "agent": e.agent,
            "query": e.payload.get("query"),
            "mode": e.payload.get("mode"),
            "results": [r["chunk_id"] for r in e.payload.get("results", [])],
        }
        for e in events
        if e.category == C.RETRIEVAL and e.payload.get("stage") == "agent"
    ]
    summary = [e.payload for e in events if e.category == C.RUN_SUMMARY]
    report = record.verification
    return {
        "id": run.id,
        "scenario": run.scenario,
        "run_id": record.run_id,
        "state": record.state.value,
        "outcome_note": record.outcome_note,
        "error": record.error,
        "seconds": seconds,
        "provider": info.name if info else record.provider,
        "origin": info.origin if info else None,
        "model": info.model if info else None,
        "weights": (info.server or {}).get("weights") if info else None,
        "prompt_version": info.prompt_version if info else None,
        "recording": info.recording if info else None,
        "model_requests": usage.model_requests,
        "input_tokens": usage.actual_input_tokens,
        "output_tokens": usage.actual_output_tokens,
        "unreported_calls": usage.unreported_calls,
        "cost": summary[-1].get("cost") if summary else None,
        "rounds": [
            {"round": s.round, "mode": s.mode.value, "rule": s.rule_id} for s in record.strategies
        ],
        "tool_calls_by_agent": calls,
        "findings": [
            {
                "round": f.get("round"),
                "agent": f.get("role"),
                "outcome": f.get("outcome"),
                "claims": f.get("hypotheses"),  # model text
                "dropped_out_of_role": f.get("dropped_out_of_role"),
            }
            for f in finishes
        ],
        "verification": None
        if report is None
        else {
            "status": report.status,
            "best": None
            if report.best is None
            else f"{report.best.mechanism.value} on {report.best.target_service}",
            "checks": [
                f"{c.hypothesis.mechanism.value} on {c.hypothesis.target_service}: "
                f"{c.hypothesis.status.value} (by {', '.join(p.value for p in c.proposers)})"
                for c in report.checks
            ],
            "disagreements": [
                {"kind": d.kind, "resolved": d.resolved, "rule": d.rule_id, "claims": d.claims}
                for d in report.disagreements
            ],
        },
        "retrieval": record.retrieval.model_dump(mode="json") if record.retrieval else None,
        "agent_searches": searches,
        "knowledge_overlays": record.versions.get("knowledge_overlays"),
        "memory_hits": [
            {
                "episode": h.episode.episode_id,
                "partition": h.episode.partition.value,
                "similarity": h.similarity,
                "safe_to_reuse": h.safe_to_reuse,
                "contradicting": h.contradicting,
            }
            for h in record.memory_hits
        ],
        "proposals": [
            f"{p.action} on {p.target} {json.dumps(p.params)} (source {p.source})"
            for p in record.proposals
        ],
        "actions": [f"{a.action} on {a.target}: {a.result}" for a in record.actions],
        "recovered": bool(record.recovery and record.recovery.verified),
        "episode_id": record.episode_id,
    }


def markdown(plan: Plan, results: list[dict[str, Any]]) -> str:
    """A readable digest; model-written text is quoted as data."""
    lines = [
        "# Live evaluation (simulated environment, simulation auto-approver)",
        "",
        f"Provider: `{plan.provider}`. Every number below comes from the run records.",
        "",
        "| Run | Scenario | State | Rounds | Requests | Tokens in / out | Fix | Recovered | Time |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        rounds = ", ".join(f"{x['mode']} ({x['rule']})" for x in r["rounds"]) or "-"
        fix = "; ".join(r["actions"]) or "none"
        tokens = f"{r['input_tokens'] or 0:,} / {r['output_tokens'] or 0:,}"
        lines.append(
            f"| {r['id']} | `{r['scenario']}` | {r['state']} | {rounds} | {r['model_requests']} "
            f"| {tokens} | {fix} | {'yes' if r['recovered'] else 'no'} | {r['seconds']} s |"
        )
    for r in results:
        lines += ["", f"## {r['id']} (`{r['run_id']}`)", ""]
        model = f"`{r['model']}` ({r['origin']}), weights `{r['weights']}`"
        lines.append(f"- Model: {model}, {r['prompt_version']}")
        lines.append(f"- Outcome: {r['state']}: {r['outcome_note']}")
        for agent, agent_calls in r["tool_calls_by_agent"].items():
            lines.append(f"- {agent} called: " + ", ".join(f"`{c}`" for c in agent_calls))
        verification = r["verification"] or {}
        for check in verification.get("checks", []):
            lines.append(f"- verifier: {check}")
        for d in verification.get("disagreements", []):
            state = "settled by " + d["rule"] if d["resolved"] else "OPEN"
            lines.append(f"- disagreement ({d['kind']}, {state}): {'; '.join(d['claims'])}")
        for s in r["agent_searches"]:
            found = ", ".join(s["results"])
            lines.append(f"- {s['agent']} searched ({s['mode']}): `{s['query']}` -> {found}")
        for h in r["memory_hits"]:
            verdict = (
                "safe to reuse"
                if h["safe_to_reuse"]
                else "rejected: " + "; ".join(h["contradicting"])
            )
            where = f"{h['partition']}, similarity {h['similarity']}"
            lines.append(f"- memory {h['episode']} ({where}): {verdict}")
        if r["recording"]:
            lines.append(f"- recording: `{r['recording']}`")
    return "\n".join(lines) + "\n"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("plan", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="where the summaries go")
    parser.add_argument("--data-dir", type=Path, help="the runs' data folder (default: OUT/data)")
    parser.add_argument("--only", action="append", default=[], help="run only these run ids")
    args = parser.parse_args(argv)

    plan = load_plan(args.plan)
    data_dir = (args.data_dir or args.out / "data").resolve()
    refused = "refused: use a separate data folder, not the repository's own data/"
    if is_repository_data(data_dir, REPO_ROOT):
        print(refused, file=sys.stderr)
        return 2
    base = load_settings(data_dir=data_dir)
    spec = base.providers.providers.get(plan.provider)
    if spec is None or spec.origin not in MODEL_ORIGINS:
        print(f"refused: {plan.provider!r} is not a local or free model provider", file=sys.stderr)
        return 2
    problems = check_plan(plan, base)
    if problems:
        print("refused: the plan cannot run as written:", *problems, sep="\n  ", file=sys.stderr)
        return 2
    created = not data_dir.exists()
    data_dir.mkdir(parents=True, exist_ok=True)
    # Again, now that the folder exists: a new "DATA" on a case-insensitive file system IS
    # data/ when data/ did not exist yet, and only now can the two be compared.
    if is_repository_data(data_dir, REPO_ROOT):
        if created:  # leave nothing behind: it would be the repository's data folder now
            with contextlib.suppress(OSError):
                data_dir.rmdir()
        print(refused, file=sys.stderr)
        return 2
    runs = [run for run in plan.runs if not args.only or run.id in args.only]
    args.out.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for run in runs:
        print(
            f"== {run.id}: {run.scenario} ({run.strategy}, retrieval {run.retrieval or 'default'})"
        )
        results.append(await run_one(base, plan, run))
        latest = results[-1]
        print(
            f"   {latest['state']} in {latest['seconds']} s: {latest['model_requests']} requests, "
            f"{latest['input_tokens']} in / {latest['output_tokens']} out tokens"
        )
        # Written after every run, so a long plan that stops still leaves its results.
        (args.out / "summary.json").write_text(json.dumps(results, indent=1) + "\n")
        (args.out / "summary.md").write_text(markdown(plan, results))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
