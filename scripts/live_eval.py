"""Run a planned set of incidents with a REAL model and summarize each one, truthfully.

    python scripts/live_eval.py evals/plans/m3-real-models.yaml --out /tmp/recap-live
    python scripts/live_eval.py PLAN --out DIR --only unfamiliar      # one run of the plan
    python scripts/live_eval.py evals/plans/m3-groq-free.yaml --out DIR --confirm-free-plan
    python scripts/live_eval.py --from-data DIR --out OUT [PLAN]      # no model is called

A plan (YAML) names the provider (ollama, or groq on its free plan: never a paid API) and
the runs, in order, in ONE data folder, so a later run can find an earlier run's episode in
memory. Per run it may force the routing strategy, choose retrieval (bm25 or hybrid), add
knowledge overlays (a red-team poisoned runbook) and plant a false memory first (both given
repo-relative). Every run is approved by the SIMULATION auto-approver; the human-approved
deliverable is a separate `scripts/demo.py --approve ask` run. The whole plan is checked
before its first run (scenarios, folders and files exist, the provider can be built), so a
typo never stops a long real-model plan halfway; the repository's own data/ folder is
refused under any spelling.

The summary (summary.json and summary.md in --out) says only what the run records say:
the state, the model, its requests and the tokens the server reported, each agent's tool
calls in order, the findings' outcomes, the disagreements, the retrieval mode and searches,
the memory verdicts, the fix and the oracle's verdict. Search queries and claims are model
text: summary.md shows them as quoted data.

Every plan run is also one evaluation batch of mode `real_llm` (evals/records.py): one
EvalRecord per run, built from the run's record and trace (evals/categorize.py), with its
failure category. It is written to --out (batch.json, and batch.md for people) and to
<data folder>/evals/batches/<batch_id>/, after every run, so a plan that stops still leaves
its batch. --publish also writes it under evals/reports/batches/ (git), refusing a batch
that names a local folder.

Cost controls, checked before anything runs:
  * never in CI: with the CI environment variable set, no live run starts (exit 2);
  * a hosted free plan (groq) needs --confirm-free-plan for each batch, on top of
    RECAP_ALLOW_LIVE_FREE_INFERENCE=1 and the owner's dated free-plan confirmation, which
    the free-tier guard checks before every request (docs/provider-options.md);
  * a quota stop ends the batch cleanly: the run it stopped is recorded (QUOTA_EXHAUSTED,
    failure category quota_cap) and the plan's later runs are not started (exit 3). So
    does the free-plan tripwire, which stops the provider.

--from-data builds the batch from the finished real-model runs already in a data folder
(oldest first) without calling any model: for runs made before batches existed, or to
re-judge them after the categories changed. With a PLAN, only that plan's runs count
(matched by their labels). A batch holds one model and one prompt version. Runs against a
real cluster (M5: the Kind target, each fix approved by a person at scripts/demo.py --approve
ask) make a `real_kubernetes` batch instead, read with that target's profile
(RECAP_TARGET=kind-demo); a folder that mixes the simulator with a cluster is refused.
--limitation adds what the runs' records cannot say themselves (repeatable).

    RECAP_TARGET=kind-demo python scripts/live_eval.py --from-data --data-dir ~/recap-kind-runs \
        --out /tmp/recap-kind-batch --limitation "..." --publish
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from pydantic import Field, ValidationError

from evals.categorize import (
    batch_markdown,
    created_at_of,
    publish_batch,
    record_from_run,
    run_events,
)
from evals.records import (
    EvalBatch,
    EvalMode,
    EvalRecord,
    make_batch,
    write_batch,
    write_batch_file,
)
from recap.core.config import REPO_ROOT, Settings, load_settings
from recap.core.errors import ConfigError
from recap.core.ids import new_id, utcnow
from recap.core.models import (
    TERMINAL_STATES,
    Contract,
    ExperienceEpisode,
    MemoryPartition,
    RunConfig,
    RunRecord,
    TraceEvent,
)
from recap.environments.registry import create_environment
from recap.harness.store import RunStore
from recap.runtime import Runtime, code_version
from recap.telemetry.sinks import SqliteTraceSink
from recap.telemetry.tracer import TraceCategory as C
from scripts.redteam_setup import is_repository_data
from scripts.redteam_setup import main as plant

MODEL_ORIGINS = ("local_llm", "free_hosted_llm")  # what a live evaluation may use
CI_ENV = "CI"  # set by CI services (GitHub Actions sets CI=true)
EXIT_REFUSED = 2  # nothing was started (or nothing was published)
EXIT_STOPPED = 3  # a quota stop or the free-plan tripwire ended the batch early
BATCH_FILE = "batch.json"
AUTO_APPROVER = "auto-approver[simulation]"  # its actor name (recap/safety/approvals.py)
REFUSED_CI = (
    "refused: live evaluations never run in CI (the CI environment variable is set); "
    "CI replays recordings instead (scripts/eval_replay.py)"
)


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


def running_in_ci(environ: dict[str, str] | None = None) -> bool:
    """True when the CI variable is set (to anything but an empty value): fail closed."""
    value = (os.environ if environ is None else environ).get(CI_ENV, "")
    return bool(value.strip())


def settings_for(base: Settings, run: PlannedRun) -> Settings:
    """The run's settings: the shared data folder, plus this run's knowledge overlays
    (resolved, as RECAP_KNOWLEDGE_OVERLAYS is, so a run records a folder outside the
    repository by its own name)."""
    overlays = [(REPO_ROOT / folder).resolve() for folder in run.overlays]
    paths = base.paths.model_copy(update={"knowledge_overlays": overlays})
    return base.model_copy(update={"paths": paths})


async def run_one(base: Settings, plan: Plan, run: PlannedRun) -> tuple[dict[str, Any], EvalRecord]:
    """Run one planned incident; return its summary and its evaluation record."""
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
    events = run_events(runtime.events, record.run_id)
    scenario = next((s for s in runtime.list_scenarios() if s.id == run.scenario), None)
    evaluated = record_from_run(
        record,
        events,
        mode="real_llm",
        scenario=scenario,
        evidence=runtime.runs.evidence(record.run_id),
    )
    return summarize(run, record, events, seconds, settings), evaluated


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


# --- the evaluation batch ---------------------------------------------------------------------


def batch_stop(record: EvalRecord) -> str:
    """Why the plan's later runs must not start ("" when they may)."""
    if record.failure == "quota_cap":
        return "the free plan's quota (or RECAP's own cap below it) ran out"
    if "FreeStatusLost" in record.failure_detail:
        return "the free-plan tripwire stopped the provider"
    return ""


def real_llm_batch(
    *,
    batch_id: str,
    title: str,
    records: list[EvalRecord],
    plan: str | None,
    started: datetime,
    methods: list[str],
    extra_limitations: list[str],
    mode: EvalMode = "real_llm",
) -> EvalBatch:
    """Real-model runs as one batch (`real_llm`, or `real_kubernetes` on a real cluster),
    with how to read it."""
    return make_batch(
        batch_id=batch_id,
        mode=mode,
        title=title,
        records=records,
        created_at=created_at_of(records, started),
        plan=plan,
        methods=[*methods, *common_methods(records)],
        limitations=[*sample_limitations(records, mode), *extra_limitations],
    )


def common_methods(records: list[EvalRecord]) -> list[str]:
    """How every real-model batch's numbers were made and judged."""
    approvals = sorted({line.split(" (")[0] for r in records for line in r.approvals})
    return [
        "Each record is built from the run's own record and trace (evals/categorize.py): "
        "success only when the independent oracle verified the recovery; any other end "
        "carries one failure category, judged against the scenario's predeclared answer, "
        "which the model never sees.",
        "Approvals: " + ("; ".join(approvals) if approvals else "none were requested") + ".",
        "Tokens in / out are the counts the model server reported; est. tokens is RECAP's "
        "own estimate of the context it built. Cost: $0 (a local model, or a free plan "
        "checked before every request).",
        "model_seconds sums each request's duration as RECAP measured it. When two "
        "specialists ask one local server that answers one request at a time, a request waits "
        "in its queue, so the sum can exceed the run's own time. The trace's llm.call events "
        "carry the server's own time (server_ms, Ollama's total_duration) for runs made since "
        "M4: model_server_seconds sums it, and model_wait_seconds sums the part of it a "
        "request spent waiting inside the server instead of computing (server time minus "
        "Ollama's own load, prompt and generation time).",
    ]


def sample_limitations(records: list[EvalRecord], mode: EvalMode = "real_llm") -> list[str]:
    """What a small real-model batch cannot show (always shown with it)."""
    per_scenario: dict[str, int] = {}
    for record in records:
        per_scenario[record.scenario_id] = per_scenario.get(record.scenario_id, 0) + 1
    counts = ", ".join(f"{scenario} {n}" for scenario, n in per_scenario.items())
    return [
        f"N = {len(records)} ({counts or 'no runs'}): each run is one sample of one "
        "configuration. These are counts, not success rates, and no statistical claim is made.",
        TARGETS.get(mode, TARGETS["real_llm"]),
        REPEATS.get(mode, REPEATS["real_llm"]),
        _review_limitation(records),
    ]


# Whether a mode's runs can be repeated (sample_limitations).
REPEATS: dict[str, str] = {
    "real_llm": "A model's run is not exactly repeatable (random IDs in its prompts change the "
    "input); only a replay of its recording is.",
    "real_kubernetes": "A model's run is not exactly repeatable (random IDs in its prompts "
    "change the input), and a run on a real cluster cannot be replayed (a replay is refused "
    "against a live target): each record stands for its own run.",
}

# What each mode's target is (sample_limitations).
TARGETS: dict[str, str] = {
    "real_llm": "The target is RECAP's SIMULATED shop, not a real cluster (runs against a real "
    "cluster are the real_kubernetes mode).",
    "real_kubernetes": "The target is a real Kubernetes cluster (Kind): two demo services and "
    "the faults RECAP's scenarios inject, with real pods, real reads and real writes, but not "
    "production traffic, and one small cluster.",
}


def _review_limitation(records: list[EvalRecord]) -> str:
    """Whether a person's review is part of these runs (from who approved each fix)."""
    decided = [r for r in records if r.approvals]
    by_simulator = [r for r in decided if all(AUTO_APPROVER in a for a in r.approvals)]
    if not decided:
        return "No fix reached an approval, so no run tested the approval step."
    if len(by_simulator) == len(decided):
        return (
            "Every fix was approved by the simulation auto-approver: these runs do not test "
            "human review."
        )
    if not by_simulator:
        return (
            "Every fix that reached an approval was decided by someone other than the "
            "simulation auto-approver (each record's approvals say who and how)."
        )
    return (
        f"{len(decided) - len(by_simulator)} of the {len(decided)} fixes that reached an "
        "approval were decided by someone other than the simulation auto-approver (each "
        "record's approvals say who); the others do not test human review."
    )


def new_batch_id(started: datetime, provider: str, plan_path: Path) -> str:
    """A live batch's id: when it started, its provider and its plan's name."""
    suffix = new_id("batch").split("-")[-1][:6]
    return f"{started:%Y%m%d-%H%M%S}-real-llm-{_slug(provider)}-{_slug(plan_path.stem)}-{suffix}"


def write_outputs(batch: EvalBatch, out: Path, data_dir: Path) -> None:
    """The batch in --out (batch.json, batch.md) and in the data folder's evals/batches/."""
    write_batch_file(batch, out / BATCH_FILE)
    (out / "batch.md").write_text(batch_markdown(batch), encoding="utf-8")
    write_batch(batch, data_dir / "evals" / "batches")


def publish(batch: EvalBatch) -> int:
    """--publish: the batch under evals/reports/batches/ (git); refused if it names a local
    folder or holds a secret-shaped value."""
    try:
        path = publish_batch(batch)
    except ValueError as exc:
        print(f"not published: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"published {_shown(path.parent)}")
    return 0


# --- rebuilding a batch from runs already made ------------------------------------------------


def rebuild(args: argparse.Namespace) -> int:
    """--from-data: the batch of the finished real-model runs in a data folder (no model call)."""
    if args.data_dir is None:
        print("refused: --from-data reads the runs of --data-dir; name the folder", file=sys.stderr)
        return EXIT_REFUSED
    data_dir = args.data_dir.resolve()
    database = data_dir / "recap.db"
    if not database.is_file():
        print(f"refused: {data_dir.name} holds no runs (no recap.db)", file=sys.stderr)
        return EXIT_REFUSED
    plan = load_plan(args.plan) if args.plan is not None else None
    store = RunStore(database)
    runs = sorted(store.list(limit=1_000_000), key=lambda r: r.created_at)
    finished = [r for r in runs if r.state in TERMINAL_STATES and _origin(r) in MODEL_ORIGINS]
    missing: list[str] = []
    if plan is not None:
        labels = {f"live-eval:{run.id}" for run in plan.runs}
        finished = [r for r in finished if r.config.label in labels]
        found = {r.config.label for r in finished}
        missing = [run.id for run in plan.runs if f"live-eval:{run.id}" not in found]
    if not finished:
        print("refused: no finished real-model run to evaluate in that folder", file=sys.stderr)
        return EXIT_REFUSED
    targets = sorted({_live_target(r) or "" for r in finished})
    if len(targets) > 1:
        shown = ", ".join(target or "the simulator" for target in targets)
        print(
            f"refused: the runs worked on more than one target ({shown}); a batch holds runs "
            "of one",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    live = targets[0]
    mode: EvalMode = "real_kubernetes" if live else "real_llm"
    kinds = sorted({(_model(r), _prompts(r)) for r in finished})
    if len(kinds) > 1:
        shown = "; ".join(f"{model} {prompts}" for model, prompts in kinds)
        print(
            f"refused: the runs used more than one model or prompt version ({shown}); a batch "
            "holds one (name a plan, or use a folder with one kind of run)",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    settings = load_settings(data_dir=data_dir)
    if live and settings.environment.kind != live:
        print(
            f"refused: the runs worked on {live!r}, but this configuration targets "
            f"{settings.environment.kind!r}: read them with that target's profile (for Kind, "
            "RECAP_TARGET=kind-demo), or their scenarios are unknown here",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    environment = create_environment(settings.environment, "describe")
    scenarios = {s.id: s for s in environment.list_scenarios()}
    sink = SqliteTraceSink(database)
    records = [
        record_from_run(
            run,
            run_events(sink, run.run_id),
            mode=mode,
            scenario=scenarios.get(run.scenario_id),
            evidence=store.evidence(run.run_id),
        )
        for run in finished
    ]
    model, prompts = kinds[0]
    first = created_at_of(records, utcnow())
    slug = mode.replace("_", "-")
    batch_id = args.batch_id or f"{first:%Y-%m-%d}-{slug}-{_slug(model)}-{_slug(prompts)}"
    title = (
        f"Real model on a real Kubernetes cluster ({live}): {model}, {prompts}"
        if live
        else f"Real model on simulated incidents: {model}, {prompts}"
    )
    plan_shown = _shown(args.plan) if args.plan is not None else None
    made = _how_made(finished, plan_shown)
    rebuilt = (
        f"Rebuilt on {utcnow():%Y-%m-%d} from the runs' own records and traces with "
        f"scripts/live_eval.py --from-data (no model was called); categories by "
        f"evals/categorize.py at code {code_version(REPO_ROOT)}."
    )
    batch = real_llm_batch(
        batch_id=batch_id,
        title=args.title or title,
        records=records,
        plan=plan_shown,
        started=first,
        methods=[made, rebuilt],
        extra_limitations=[
            *([f"Planned but not in this folder: {', '.join(missing)}."] if missing else []),
            *args.limitation,
        ],
        mode=mode,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    write_outputs(batch, args.out, data_dir)
    print(f"{batch.batch_id}: {batch.n} run{'' if batch.n == 1 else 's'}, mode {mode}")
    return publish(batch) if args.publish else 0


def _how_made(runs: list[RunRecord], plan_shown: str | None) -> str:
    """How rebuilt runs were made, from what their own records say (their labels)."""
    if plan_shown is not None:
        return (
            f"Plan {plan_shown}: its runs in one data folder, in order "
            "(scripts/live_eval.py, every run labelled live-eval:<run id>)."
        )
    labels = [r.config.label for r in runs]
    planned = [label for label in labels if label.startswith("live-eval:")]
    if planned and len(planned) == len(labels):
        return (
            f"Runs of a live-evaluation plan that is not in the repository (labels "
            f"{', '.join(planned)}), in one data folder, in the order they ran; each record "
            "shows its own scenario, routing and retrieval."
        )
    return (
        "Runs started without a plan (no live-eval label: scripts/demo.py or the API), in one "
        "data folder, in the order they ran."
    )


# --- main ----------------------------------------------------------------------------------------


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("plan", type=Path, nargs="?", help="the plan (optional with --from-data)")
    parser.add_argument("--out", type=Path, required=True, help="where the summaries go")
    parser.add_argument("--data-dir", type=Path, help="the runs' data folder (default: OUT/data)")
    parser.add_argument("--only", action="append", default=[], help="run only these run ids")
    parser.add_argument(
        "--confirm-free-plan",
        action="store_true",
        help="a hosted free plan: confirm, for this batch, that it runs on the free plan and "
        "may use its free quota (the free-tier guard's own checks still apply)",
    )
    parser.add_argument(
        "--from-data",
        action="store_true",
        help="build the batch from the finished real-model runs in --data-dir (no model call)",
    )
    parser.add_argument("--batch-id", help="the batch's id (default: from its date and model)")
    parser.add_argument("--title", help="the batch's title (--from-data)")
    parser.add_argument(
        "--limitation",
        action="append",
        default=[],
        help="a limitation the runs' records cannot state themselves (--from-data; repeatable)",
    )
    parser.add_argument(
        "--publish", action="store_true", help="also publish the batch in evals/reports/batches/"
    )
    args = parser.parse_args(argv)

    if args.from_data:
        return rebuild(args)
    if args.plan is None:
        parser.error("a plan is needed (or --from-data with --data-dir)")
    if running_in_ci():
        print(REFUSED_CI, file=sys.stderr)
        return EXIT_REFUSED
    plan = load_plan(args.plan)
    data_dir = (args.data_dir or args.out / "data").resolve()
    refused = "refused: use a separate data folder, not the repository's own data/"
    if is_repository_data(data_dir, REPO_ROOT):
        print(refused, file=sys.stderr)
        return EXIT_REFUSED
    base = load_settings(data_dir=data_dir)
    spec = base.providers.providers.get(plan.provider)
    if spec is None or spec.origin not in MODEL_ORIGINS:
        print(f"refused: {plan.provider!r} is not a local or free model provider", file=sys.stderr)
        return EXIT_REFUSED
    if spec.origin == "free_hosted_llm" and not args.confirm_free_plan:
        print(
            f"refused: {plan.provider} is a hosted free plan. Pass --confirm-free-plan to "
            "confirm, for this batch, that the account is on the free plan and that the batch "
            "may use its free quota (RECAP_ALLOW_LIVE_FREE_INFERENCE=1 and the dated "
            "confirmation are still checked before every request)",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    problems = check_plan(plan, base)
    runs = [run for run in plan.runs if not args.only or run.id in args.only]
    if not runs:
        problems.append(f"no run of the plan is named {', '.join(args.only)}")
    if problems:
        print("refused: the plan cannot run as written:", *problems, sep="\n  ", file=sys.stderr)
        return EXIT_REFUSED
    created = not data_dir.exists()
    data_dir.mkdir(parents=True, exist_ok=True)
    # Again, now that the folder exists: a new "DATA" on a case-insensitive file system IS
    # data/ when data/ did not exist yet, and only now can the two be compared.
    if is_repository_data(data_dir, REPO_ROOT):
        if created:  # leave nothing behind: it would be the repository's data folder now
            with contextlib.suppress(OSError):
                data_dir.rmdir()
        print(refused, file=sys.stderr)
        return EXIT_REFUSED
    try:  # the provider can be built (opt-in, key, confirmation, stops): nothing is sent
        Runtime(base).make_provider(RunConfig(provider=plan.provider))
    except ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    args.out.mkdir(parents=True, exist_ok=True)
    started = utcnow()
    batch_id = args.batch_id or new_batch_id(started, plan.provider, args.plan)
    plan_shown = _shown(args.plan)
    made = (
        f"Plan {plan_shown}: {len(runs)} of its runs in one data folder, in order (a later run "
        "can find an earlier run's episode in memory), each approved by the simulation "
        "auto-approver (scripts/live_eval.py)."
    )
    results: list[dict[str, Any]] = []
    records: list[EvalRecord] = []
    for index, run in enumerate(runs):
        print(
            f"== {run.id}: {run.scenario} ({run.strategy}, retrieval {run.retrieval or 'default'})"
        )
        summary, record = await run_one(base, plan, run)
        results.append(summary)
        records.append(record)
        print(
            f"   {summary['state']} in {summary['seconds']} s: {summary['model_requests']} "
            f"requests, {summary['input_tokens']} in / {summary['output_tokens']} out tokens "
            f"({'success' if record.success else record.failure})"
        )
        stop = batch_stop(record)
        later = [r.id for r in runs[index + 1 :]] if stop else []
        not_started = (
            [f"Stopped after {run.id}: {stop}; not started: {', '.join(later)}."] if later else []
        )
        batch = real_llm_batch(
            batch_id=batch_id,
            title=f"Real model on simulated incidents: {plan.provider}, plan {args.plan.stem}",
            records=records,
            plan=plan_shown,
            started=started,
            methods=[made],
            extra_limitations=not_started,
        )
        # Written after every run, so a long plan that stops still leaves its results.
        (args.out / "summary.json").write_text(json.dumps(results, indent=1) + "\n")
        (args.out / "summary.md").write_text(markdown(plan, results))
        write_outputs(batch, args.out, data_dir)
        if stop:
            print(f"stopped: {stop}; not started: {', '.join(later) or 'none'}", file=sys.stderr)
            if args.publish:
                publish(batch)
            return EXIT_STOPPED
    if args.publish:
        return publish(batch)
    return 0


# --- small helpers --------------------------------------------------------------------------------


def _origin(record: RunRecord) -> str:
    info = record.provider_info
    return info.origin if info else str(record.versions.get("provider_origin", ""))


def _live_target(record: RunRecord) -> str | None:
    """The kind of the real target a run worked on (None: the simulator)."""
    target = record.environment
    return target.kind if target is not None and not target.is_simulated else None


def _model(record: RunRecord) -> str:
    info = record.provider_info
    return (info.model if info else None) or "unknown model"


def _prompts(record: RunRecord) -> str:
    info = record.provider_info
    return (info.prompt_version if info else None) or "unknown prompts"


def _slug(text: str) -> str:
    """'openai/gpt-oss-120b' -> 'openai-gpt-oss-120b'; 'prompts-v4.7' stays."""
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")


def _shown(path: Path) -> str:
    """A repo-relative path, or the file's own name: local folders never reach a batch."""
    resolved = path.resolve()
    return (
        resolved.relative_to(REPO_ROOT).as_posix()
        if resolved.is_relative_to(REPO_ROOT)
        else path.name
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
