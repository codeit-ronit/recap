"""Evaluation records and batches (M4): one record per run, one batch per evaluation plan.

Every evaluation mode writes the same record, and a batch holds runs of ONE mode only, so
their numbers are never mixed:

  scripted_regression  the scripted policy on the V1 benchmark (evals/runner.py): an
                       integration regression of RECAP's own code, not evidence about LLMs
  offline_replay       a recorded real-model run replayed offline (fixtures/recordings/): it
                       proves the recorded decisions still pass RECAP's path, not a new run
  real_llm             a real model (local, or a verified free plan) on simulated incidents
                       (scripts/live_eval.py)
  real_kubernetes      a real model on a real Kubernetes cluster (M5: the Kind demo cluster or
                       SREGym's own, told apart by `target`), with real faults, a person's
                       approval for every write and the cluster's own health as the oracle
                       (scripts/live_eval.py --from-data)

A record says only what the run's record and trace say. A run counts as a success only when
the independent oracle verified the recovery; anything else carries a failure category.
`expected` is the scenario's predeclared answer, which the model never sees.

Where batches live: evals/reports/batches/<batch_id>/batch.json (published, in git) and
<data_dir>/evals/batches/<batch_id>/batch.json (local runs, never in git).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from recap.core.models import Contract

SCHEMA = "recap-eval-batch-v1"

EvalMode = Literal["scripted_regression", "offline_replay", "real_llm", "real_kubernetes"]

# Why a run did not end in a verified recovery ("" for a verified success).
FailureCategory = Literal[
    "",
    "missed_crucial_tool",  # never read the evidence that shows the cause
    "wrong_mechanism",  # named the right service with the wrong failure mechanism
    "wrong_service",  # put the fault on another service
    "insufficient_evidence",  # claims stayed unsupported after the allowed rounds
    "open_disagreement",  # supported causes still conflicted: a person decides
    "budget_exhausted",  # a run limit (steps, tool calls, tokens, time) stopped it
    "quota_cap",  # a free plan's (or RECAP's own) rate cap deferred it
    "safety_blocked",  # the policy or the evidence gate refused the proposed fix
    "provider_failure",  # the model server failed or gave no usable answer
    "oracle_not_recovered",  # a fix ran, the oracle found the target still unhealthy
    "approval_rejected",  # the person said no, or the approval expired
    "cancelled",  # someone stopped the run
    "error",  # anything else (the record names it)
]


class StageTiming(Contract):
    """How long one stage of a run took, from its state transitions."""

    stage: str
    seconds: float


class EvalRecord(Contract):
    """One run, as an evaluation sees it."""

    run_id: str
    mode: EvalMode
    scenario_id: str
    incident_id: str = ""
    incident_family: str = ""
    split: str = ""  # demo, train or test (held-out scenarios are test)
    expected: dict[str, Any] = Field(default_factory=dict)  # predeclared; hidden from the model
    provider: str = ""
    provider_class: str = ""  # scripted, local_llm, free_hosted_llm or replay
    model: str | None = None
    model_weights: str | None = None
    prompt_version: str | None = None
    config_hash: str = ""
    code_version: str | None = None
    strategy: str = ""
    topology: list[str] = Field(default_factory=list)  # per round, e.g. "parallel: a + b"
    rounds: int = 0
    retrieval_mode: str = ""
    memory: bool = False
    model_requests: int = 0
    input_tokens: int | None = None  # as servers reported them; None when none reported
    output_tokens: int | None = None
    estimated_tokens: int = 0  # RECAP's own estimate of the context it built
    tool_calls: int = 0
    tool_calls_by_agent: dict[str, int] = Field(default_factory=dict)
    retrieved_docs: list[str] = Field(default_factory=list)
    agent_searches: int = 0
    memory_hits: list[dict[str, Any]] = Field(default_factory=list)
    supported_hypotheses: list[str] = Field(default_factory=list)  # "mechanism@service"
    diagnosis: str | None = None  # the verifier's best, "mechanism@service"
    diagnosis_correct: bool | None = None  # None when the scenario declares no answer
    proposed_actions: list[str] = Field(default_factory=list)
    gateway_outcome: str = ""  # what the policy decided for the proposed fix
    approvals: list[str] = Field(default_factory=list)  # "approved by X" / "rejected by X"
    final_state: str = ""
    oracle_verified: bool = False
    success: bool = False  # True ONLY with the oracle's verified recovery
    failure: FailureCategory = ""
    failure_detail: str = ""
    seconds: float = 0.0
    stages: list[StageTiming] = Field(default_factory=list)
    model_seconds: float | None = None  # summed request durations (requests may queue)
    # Over live requests that report them: the model server's own time, and the part of it
    # a request waited inside the server instead of computing (queued behind another agent's
    # request on a server that answers one at a time, or loading). None when not reported.
    model_server_seconds: float | None = None
    model_wait_seconds: float | None = None
    label: str = ""  # the run's label: an eval configuration, live-eval:<id>, eval-replay:<id>
    started_at: datetime | None = None
    ended_at: datetime | None = None
    recording: str | None = None  # the recording's id or file name, never a local path
    target: str = ""  # what the run worked on: "simulated", or a real cluster's kind (M5)
    # A benchmark's own grader (SREGym's run-oracle.py), recorded by hand after the run: the
    # benchmark's verdict, kept apart from RECAP's (`success`) and never merged into it.
    benchmark_verdict: Literal["pass", "fail"] | None = None


class EvalBatch(Contract):
    """One evaluation plan's runs, all of one mode."""

    schema_version: str = SCHEMA
    batch_id: str
    mode: EvalMode
    title: str
    created_at: datetime
    plan: str | None = None  # the plan file (repo-relative) or the runner's configuration
    providers: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    records: list[EvalRecord] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)  # how the runs were made and judged
    limitations: list[str] = Field(default_factory=list)  # what the numbers do not show
    targets: list[str] = Field(default_factory=list)  # its records' targets ("simulated", "kind")
    benchmark: str | None = None  # the grader behind the records' benchmark_verdict

    @property
    def n(self) -> int:
        return len(self.records)


def make_batch(
    *,
    batch_id: str,
    mode: EvalMode,
    title: str,
    records: Iterable[EvalRecord],
    created_at: datetime,
    plan: str | None = None,
    methods: Iterable[str] = (),
    limitations: Iterable[str] = (),
    benchmark: str | None = None,
) -> EvalBatch:
    """A batch of ONE mode, with its providers, models and prompt versions read from its records.

    Refuses (ValueError) a record of another mode, so two modes can never share a batch's
    numbers.
    """
    kept = list(records)
    other = [r.run_id for r in kept if r.mode != mode]
    if other:
        raise ValueError(f"a {mode} batch cannot hold runs of another mode: {', '.join(other)}")
    return EvalBatch(
        batch_id=batch_id,
        mode=mode,
        title=title,
        created_at=created_at,
        plan=plan,
        providers=sorted({r.provider for r in kept if r.provider}),
        models=sorted({r.model for r in kept if r.model}),
        prompt_versions=sorted({r.prompt_version for r in kept if r.prompt_version}),
        records=kept,
        methods=list(methods),
        limitations=list(limitations),
        targets=sorted({r.target for r in kept if r.target}),
        benchmark=benchmark,
    )


def load_batch(path: Path) -> EvalBatch:
    """Read one batch.json; a wrong schema is refused, never guessed."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA:
        raise ValueError(f"{path}: not a {SCHEMA} file")
    return EvalBatch.model_validate(data)


def find_batches(roots: Iterable[Path]) -> list[EvalBatch]:
    """Every batch under the given folders (each <root>/<batch_id>/batch.json), newest first.

    A folder that does not exist is skipped; a broken file is skipped too (the loader of a
    dashboard must not fail because one local file is half written).
    """
    found: list[EvalBatch] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/batch.json")):
            try:
                found.append(load_batch(path))
            except (ValueError, OSError):
                continue
    return sorted(found, key=lambda b: b.created_at, reverse=True)


def write_batch(batch: EvalBatch, root: Path) -> Path:
    """Write <root>/<batch_id>/batch.json and return its path."""
    return write_batch_file(batch, root / batch.batch_id / "batch.json")


def write_batch_file(batch: EvalBatch, path: Path) -> Path:
    """Write one batch to `path` (for example a report folder's batch.json) and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(batch.model_dump_json(indent=1) + "\n", encoding="utf-8")
    return path
