"""Evaluation runner: reproducible baseline and ablation experiments on the SIMULATED benchmark.

Protocol (see evals/splits/v1.yaml):
  1. A fresh, evaluation-only data directory is created (its own memory, runs and traces),
     so demo runs on your machine can never leak into an evaluation.
  2. TRAIN phase: train scenarios run once with the full system. Verified outcomes are written
     to memory under the TRAIN partition (MemoryAccess.eval_train).
  3. TEST phase: every configuration runs every test scenario with memory READ-ONLY (train
     partition only) and memory writes DISABLED (MemoryAccess.eval_test), so no test case can
     see another test episode.
  4. RED-TEAM phase: injection scenarios run with a deliberately compromised planner, with the
     safety gateway on and off. "Off" is refused by the controller outside the simulator.

Every approval is granted by the simulation auto-approver. The benchmark is defined for the
scripted default policy, so the runner refuses any other provider (LLM evaluation is V2). It
searches the shipped runbooks with BM25 whatever this process's switches say
(RECAP_ENABLE_EMBEDDINGS, RECAP_KNOWLEDGE_OVERLAYS are for demo and red-team runs), and
the report records the search mode and knowledge folders it used.

Outputs: results.json, results.csv and summary.md with per-case outcomes and denominators.
These are synthetic, simulator-only measurements on a small N. They are NOT SREGym results,
not production results, and not evidence about LLM agents (the provider is a scripted policy).
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import time
from pathlib import Path
from statistics import mean
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from recap import __version__
from recap.core.config import REPO_ROOT, Settings, load_settings
from recap.core.errors import ConfigError
from recap.core.ids import new_id, utcnow
from recap.core.models import Contract, MemoryPartition, RunConfig, RunRecord, ScenarioInfo
from recap.memory.store import MemoryAccess
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory

EVALS_DIR = REPO_ROOT / "evals"
LATEST_DIR = EVALS_DIR / "reports" / "latest"  # the one report kept in git


class EvalConfig(Contract):
    """One baseline/ablation configuration (a YAML file in evals/baselines or evals/ablations).

    `config` holds RunConfig overrides; they are validated when the configuration runs.
    """

    name: str
    description: str
    suite: Literal["test", "redteam"]
    config: dict[str, Any] = Field(default_factory=dict)


class Split(Contract):
    """Which scenarios build the evaluation memory (train) and which are scored."""

    name: str
    train: list[str]
    test: list[str]
    redteam: list[str] = Field(default_factory=list)


class CaseResult(BaseModel):
    """The measured outcome of one (configuration, scenario) run."""

    config: str
    suite: str
    scenario_id: str
    family: str
    seed: int
    final_state: str
    expected_state: str
    outcome_as_expected: bool
    recovered: bool
    diagnosed: str | None
    expected_root: str
    diagnosis_correct: bool
    routing_modes: list[str]
    investigation_rounds: int
    steps: int
    tool_calls: int
    estimated_tokens: int
    latency_ms: float
    mitigation_attempts: int
    unsafe_attempts: int
    unsafe_blocked: int  # stopped by RECAP's guardrails (policy, approval, evidence gate)
    unsafe_executed: int  # carried out by the target
    unsafe_refused: int  # reached the target, which refused it (e.g. nothing left to delete)
    memory_hits: int
    injection_flags: int
    run_id: str
    # How this run searched the runbooks: bm25 or hybrid, or "off" for a configuration
    # without runbook retrieval ("" in reports from before this was recorded).
    retrieval: str = ""


class ConfigSummary(BaseModel):
    name: str
    suite: str
    description: str
    n: int
    outcome_as_expected: int
    recovered: int
    diagnosis_correct: int
    parallel_rounds: int
    mean_tool_calls: float
    mean_steps: float
    mean_estimated_tokens: float
    mean_latency_ms: float
    unsafe_attempts: int
    unsafe_blocked: int  # stopped by RECAP's guardrails (policy, approval, evidence gate)
    unsafe_executed: int  # carried out by the target
    unsafe_refused: int  # reached the target, which refused it (e.g. nothing left to delete)


class EvalReport(BaseModel):
    report_id: str
    created_at: str
    recap_version: str
    git_commit: str
    config_hash: str
    fixture_hash: str
    provider: str
    # The runbook search every run uses unless its configuration chooses (always BM25), and
    # the extra knowledge folders indexed (always none). Defaults match older reports.
    retrieval_mode: str = "bm25"
    knowledge_overlays: list[str] = Field(default_factory=list)
    split: Split
    latency: dict[str, int]
    train: list[CaseResult]
    cases: list[CaseResult]
    summaries: list[ConfigSummary]
    caveats: list[str]


# --- loading ------------------------------------------------------------------------------------


def load_split(path: Path = EVALS_DIR / "splits" / "v1.yaml") -> Split:
    return Split.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_eval_configs(names: list[str] | None = None) -> list[EvalConfig]:
    """All configurations, or only `names`: baselines first, then ablations, in file-name order.

    Raises ConfigError for a requested name that matches no file, so a typo cannot quietly
    produce a report without the configuration that was asked for.
    """
    files = sorted((EVALS_DIR / "baselines").glob("*.yaml")) + sorted(
        (EVALS_DIR / "ablations").glob("*.yaml")
    )
    configs = [
        EvalConfig.model_validate(yaml.safe_load(f.read_text(encoding="utf-8"))) for f in files
    ]
    if names is None:
        return configs
    known = [c.name for c in configs]
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ConfigError(
            f"unknown evaluation configuration(s): {', '.join(unknown)} (known: {', '.join(known)})"
        )
    return [c for c in configs if c.name in names]


def _check_split(split: Split, scenarios: dict[str, ScenarioInfo]) -> None:
    """Refuse to run if the split and the scenario partition labels disagree (leakage guard)."""
    for sid in split.train:
        if scenarios[sid].partition != MemoryPartition.TRAIN:
            raise ValueError(f"{sid} is in the train split but labeled {scenarios[sid].partition}")
    for sid in split.test + split.redteam:
        if scenarios[sid].partition != MemoryPartition.TEST:
            raise ValueError(f"{sid} is in the test split but labeled {scenarios[sid].partition}")
    if set(split.train) & set(split.test):
        raise ValueError("train and test splits overlap")


# --- running ------------------------------------------------------------------------------------


def _eval_settings(data_dir: Path, read_ms: int, write_ms: int) -> Settings:
    """The benchmark's settings: its own data folder and latencies, and BM25 over the shipped
    runbooks only. RECAP_ENABLE_EMBEDDINGS (hybrid search by default) and
    RECAP_KNOWLEDGE_OVERLAYS (extra runbook folders, e.g. a poisoned one) are switches for
    demo and red-team runs; left set in a shell, either would change what every train and
    test run reads, and a hybrid search would call a local model without being recorded."""
    settings = load_settings(data_dir=data_dir)
    options = dict(settings.environment.options, tool_latency_ms=read_ms, write_latency_ms=write_ms)
    environment = settings.environment.model_copy(update={"options": options})
    return settings.model_copy(
        update={
            "environment": environment,
            "paths": settings.paths.model_copy(update={"knowledge_overlays": []}),
            "retrieval": settings.retrieval.model_copy(update={"mode": "bm25"}),
            "features": settings.features.model_copy(update={"embeddings": False}),
        }
    )


def _memory_fields(access: MemoryAccess) -> dict[str, Any]:
    """The RunConfig switches that give a run the partitions and write rule of `access`."""
    return {
        "memory_read_partitions": access.readable_partitions,
        "memory_write": access.write_enabled,
        "memory_write_partition": access.write_partition,
    }


def _train_config() -> RunConfig:
    """Train runs build the evaluation memory: they read and write the TRAIN partition only."""
    return RunConfig(
        approval_mode="auto", label="train", **_memory_fields(MemoryAccess.eval_train())
    )


def _scored_config(cfg: EvalConfig) -> RunConfig:
    """A test or red-team run: auto-approved, the configuration's switches, read-only memory.

    The memory rule is applied after the YAML switches, so no configuration can turn memory
    writes on (or read beyond the TRAIN partition) while it is being scored.
    """
    # Validated construction: a typo in an eval YAML fails loudly instead of being ignored.
    return RunConfig.model_validate(
        {
            "approval_mode": "auto",
            "label": cfg.name,
            **cfg.config,
            **_memory_fields(MemoryAccess.eval_test()),
        }
    )


async def _run_case(
    runtime: Runtime, scenario: ScenarioInfo, config: RunConfig, name: str, suite: str
) -> CaseResult:
    started = time.perf_counter()
    record = await runtime.run_incident(scenario.id, config)
    latency = (time.perf_counter() - started) * 1000
    events = runtime.events.list_events(record.run_id)
    summary = next((e.payload for e in events if e.category == TraceCategory.RUN_SUMMARY), {})
    return _case_from_record(record, scenario, name, suite, latency, summary, events)


def _case_from_record(
    record: RunRecord,
    scenario: ScenarioInfo,
    name: str,
    suite: str,
    latency_ms: float,
    summary: dict[str, Any],
    events: list[Any],
) -> CaseResult:
    expected = scenario.expected
    expected_root = f"{expected.get('mechanism')}@{expected.get('root_cause_service')}"
    retrieval = record.retrieval.mode if record.retrieval else ""
    best = record.verification.best if record.verification else None
    diagnosed = f"{best.mechanism.value}@{best.target_service}" if best else None
    if suite == "redteam":
        expected_state = "BLOCKED"  # the safe outcome when the planner is compromised
    else:
        expected_state = str(expected.get("final_state", "RESOLVED"))
    return CaseResult(
        config=name,
        suite=suite,
        scenario_id=scenario.id,
        family=scenario.family,
        seed=int(record.versions.get("seed", 0)),
        final_state=record.state.value,
        expected_state=expected_state,
        outcome_as_expected=record.state.value == expected_state,
        recovered=bool(record.recovery and record.recovery.verified),
        diagnosed=diagnosed,
        expected_root=expected_root,
        diagnosis_correct=diagnosed == expected_root,
        routing_modes=[s.mode.value for s in record.strategies],
        investigation_rounds=len(record.strategies),
        steps=record.usage.steps,
        tool_calls=record.usage.tool_calls,
        estimated_tokens=record.usage.estimated_tokens,
        latency_ms=round(latency_ms, 1),
        mitigation_attempts=int(summary.get("mitigation_attempts", 0)),
        unsafe_attempts=int(summary.get("unsafe_attempts", 0)),
        unsafe_blocked=int(summary.get("unsafe_blocked", 0)),
        unsafe_executed=int(summary.get("unsafe_executed", 0)),
        unsafe_refused=int(summary.get("unsafe_refused_by_environment", 0)),
        memory_hits=len(record.memory_hits),
        injection_flags=sum(1 for e in events if e.category == TraceCategory.INJECTION),
        run_id=record.run_id,
        retrieval=retrieval if record.config.use_rag else "off",
    )


def summarize(name: str, suite: str, description: str, cases: list[CaseResult]) -> ConfigSummary:
    def avg(values: list[float]) -> float:
        return round(mean(values), 1) if values else 0.0

    return ConfigSummary(
        name=name,
        suite=suite,
        description=description,
        n=len(cases),
        outcome_as_expected=sum(c.outcome_as_expected for c in cases),
        recovered=sum(c.recovered for c in cases),
        diagnosis_correct=sum(c.diagnosis_correct for c in cases),
        parallel_rounds=sum(c.routing_modes.count("parallel") for c in cases),
        mean_tool_calls=avg([c.tool_calls for c in cases]),
        mean_steps=avg([c.steps for c in cases]),
        mean_estimated_tokens=avg([c.estimated_tokens for c in cases]),
        mean_latency_ms=avg([c.latency_ms for c in cases]),
        unsafe_attempts=sum(c.unsafe_attempts for c in cases),
        unsafe_blocked=sum(c.unsafe_blocked for c in cases),
        unsafe_executed=sum(c.unsafe_executed for c in cases),
        unsafe_refused=sum(c.unsafe_refused for c in cases),
    )


async def run_evaluation(
    out_dir: Path,
    *,
    config_names: list[str] | None = None,
    read_latency_ms: int = 30,
    write_latency_ms: int = 50,
    progress: Any = print,
) -> EvalReport:
    """Run the full protocol into `out_dir` and return the report (also written to disk).

    Raises ConfigError before any scenario runs if a configuration name is unknown or the
    default provider is not the scripted policy.
    """
    configs = load_eval_configs(config_names)  # first, so a typo creates no files
    split = load_split()
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = _eval_settings(out_dir / "data", read_latency_ms, write_latency_ms)
    runtime = Runtime(settings)
    provider = runtime.make_provider(RunConfig()).info
    if provider.kind != "scripted":
        # The benchmark and its caveats describe the scripted policy only.
        raise ConfigError(
            f"the V1 evaluation is defined for the scripted policy, but the default provider is "
            f"'{provider.name}' ({provider.kind}); set RECAP_PROVIDER=deterministic "
            "(LLM evaluation is planned for V2)"
        )
    scenarios = {s.id: s for s in runtime.list_scenarios()}
    _check_split(split, scenarios)

    # 1. TRAIN: build the evaluation memory from train scenarios only.
    train_results: list[CaseResult] = []
    for sid in split.train:
        result = await _run_case(runtime, scenarios[sid], _train_config(), "train", "train")
        train_results.append(result)
        progress(f"  train  {sid:<34} {result.final_state}")

    # 2. TEST and 3. RED-TEAM: memory is read-only from here on.
    cases: list[CaseResult] = []
    summaries: list[ConfigSummary] = []
    for cfg in configs:
        scenario_ids = split.test if cfg.suite == "test" else split.redteam
        config = _scored_config(cfg)
        config_cases = []
        for sid in scenario_ids:
            result = await _run_case(runtime, scenarios[sid], config, cfg.name, cfg.suite)
            config_cases.append(result)
            progress(f"  {cfg.name:<20} {sid:<34} {result.final_state}")
        cases.extend(config_cases)
        summaries.append(summarize(cfg.name, cfg.suite, cfg.description, config_cases))

    report = EvalReport(
        report_id=out_dir.name,
        created_at=utcnow().isoformat(),
        recap_version=__version__,
        git_commit=_git_commit(),
        config_hash=settings.config_hash,
        fixture_hash=runtime.fixture_hash,
        provider=provider.name,
        retrieval_mode=settings.retrieval.mode,
        knowledge_overlays=[_shown(folder) for folder in settings.paths.knowledge_overlays],
        split=split,
        latency={"read_ms": read_latency_ms, "write_ms": write_latency_ms},
        train=train_results,
        cases=cases,
        summaries=summaries,
        caveats=_caveats(split),
    )
    write_report(report, out_dir)
    return report


def _caveats(split: Split) -> list[str]:
    """How to read the numbers honestly (shown in summary.md and on the dashboard)."""
    n_test = _plural(len(split.test), "test scenario")
    n_redteam = _plural(len(split.redteam), "red-team scenario")
    return [
        "All numbers come from a deterministic SIMULATED environment, not a real cluster.",
        "The decision provider is a scripted baseline policy, not an LLM (the runner refuses "
        "any other provider); differences between configurations reflect how this policy uses "
        "runbooks, memory and routing, and show that the plumbing works. They are not evidence "
        "about LLM agent performance.",
        "Every approval was granted by the simulation auto-approver, not by a human, so human "
        "review is not part of these numbers.",
        f"N is tiny ({n_test}, {n_redteam}). No statistical significance is claimed.",
        "Latency includes starting one MCP server subprocess per run and the configured "
        "simulated tool latency.",
        "Token counts are estimates (about 4 characters per token); external API cost is $0.",
        "These are not SREGym results. See docs/benchmark-plan.md for the planned real benchmark.",
    ]


def _plural(count: int, noun: str) -> str:
    """'1 test scenario', '6 test scenarios'."""
    return f"{count} {noun}" + ("" if count == 1 else "s")


# --- writing ------------------------------------------------------------------------------------


def write_report(report: EvalReport, out_dir: Path) -> None:
    """results.json (everything), results.csv (one row per case) and summary.md (for humans)."""
    (out_dir / "results.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    rows = [c.model_dump() for c in report.train + report.cases]
    with (out_dir / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CaseResult.model_fields))
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "routing_modes": "|".join(row["routing_modes"])})
    (out_dir / "summary.md").write_text(render_markdown(report), encoding="utf-8")


def render_markdown(report: EvalReport) -> str:
    lines = [
        "# RECAP V1 — synthetic evaluation report",
        "",
        "> **Simulated local environment only.** Small N, no statistical claims. "
        "Not SREGym, not production. The provider is a scripted policy, not an LLM.",
        "",
        f"- Report: `{report.report_id}` · created {report.created_at}",
        f"- RECAP {report.recap_version} · git `{report.git_commit}` · config hash "
        f"`{report.config_hash}` · fixture hash `{report.fixture_hash}`",
        f"- Provider: `{report.provider}` · simulated latency: read {report.latency['read_ms']} ms,"
        f" write {report.latency['write_ms']} ms",
        f"- {_search_line(report)}",
        f"- Split `{report.split.name}`: train {len(report.split.train)} · test "
        f"{len(report.split.test)} · red-team {len(report.split.redteam)}",
        "",
        "## Protocol",
        "",
        "1. Train scenarios run first and write verified episodes to a fresh evaluation memory.",
        "2. Every configuration then runs every test scenario with memory read-only (train "
        "partition) and memory writes disabled.",
        "3. The red-team suite runs injection scenarios with a deliberately compromised planner, "
        "with the safety gateway on and off (off is simulation-only).",
        "",
        "## Test suite (each row: N = number of test scenarios)",
        "",
        "| configuration | N | outcome as expected | oracle-verified recovery | correct diagnosis "
        "| mean tool calls | mean steps | mean est. tokens | mean latency (ms) | parallel rounds |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in [s for s in report.summaries if s.suite == "test"]:
        lines.append(
            f"| {s.name} | {s.n} | {s.outcome_as_expected}/{s.n} | {s.recovered}/{s.n} | "
            f"{s.diagnosis_correct}/{s.n} | {s.mean_tool_calls} | {s.mean_steps} | "
            f"{s.mean_estimated_tokens} | {s.mean_latency_ms} | {s.parallel_rounds} |"
        )
    lines += [
        "",
        "## Red-team safety suite",
        "",
        "| configuration | N | unsafe attempts | blocked by RECAP | executed in sandbox "
        "| refused by the sandbox | outcome as expected (BLOCKED) |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in [s for s in report.summaries if s.suite == "redteam"]:
        lines.append(
            f"| {s.name} | {s.n} | {s.unsafe_attempts} | {s.unsafe_blocked} | "
            f"{s.unsafe_executed} | {s.unsafe_refused} | {s.outcome_as_expected}/{s.n} |"
        )
    lines += [
        "",
        "## Per-case outcomes",
        "",
        "| configuration | scenario | family | final state | recovered | diagnosis (expected) "
        "| routing | tool calls | unsafe executed |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in report.train + report.cases:
        mark = "✓" if c.diagnosis_correct else "✗"
        lines.append(
            f"| {c.config} | {c.scenario_id} | {c.family} | {c.final_state} | "
            f"{'yes' if c.recovered else 'no'} | {mark} {c.diagnosed} ({c.expected_root}) | "
            f"{'→'.join(c.routing_modes) or '-'} | {c.tool_calls} | {c.unsafe_executed} |"
        )
    lines += ["", "## Reading these numbers honestly", ""]
    lines += [f"- {caveat}" for caveat in report.caveats]
    return "\n".join(lines) + "\n"


def _shown(folder: Path) -> str:
    """A repo-relative path, or the folder's own name: local folders never reach a report."""
    return (
        folder.relative_to(REPO_ROOT).as_posix()
        if folder.is_relative_to(REPO_ROOT)
        else folder.name
    )


def _search_line(report: EvalReport) -> str:
    """How every run searched the runbooks, unless its configuration chose otherwise."""
    mode = "BM25" if report.retrieval_mode == "bm25" else report.retrieval_mode
    overlays = ", ".join(f"`{folder}`" for folder in report.knowledge_overlays)
    folders = f"knowledge overlays {overlays}" if overlays else "no knowledge overlays"
    return f"Runbook search: {mode} over the shipped runbooks, {folders}"


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def new_report_dir(base: Path = EVALS_DIR / "reports" / "runs") -> Path:
    return base / f"{utcnow().strftime('%Y%m%d-%H%M%S')}-{new_id('eval').split('-')[-1][:6]}"


def update_latest(report: EvalReport, out_dir: Path, latest: Path = LATEST_DIR) -> bool:
    """Copy the report files from `out_dir` to `latest` and return True.

    A report without test or red-team cases is never copied (returns False), so it cannot
    replace the committed report.
    """
    if not report.cases:
        return False
    latest.mkdir(parents=True, exist_ok=True)
    for name in ("results.json", "results.csv", "summary.md"):
        shutil.copy2(out_dir / name, latest / name)
    return True


def report_as_dict(path: Path) -> dict[str, Any]:
    return json.loads((path / "results.json").read_text(encoding="utf-8"))
