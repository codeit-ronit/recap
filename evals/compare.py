"""Paired comparisons of evaluation configurations (M6 W4).

A comparison names two configurations of one batch that differ in ONE RunConfig switch
(evals/comparisons/*.yaml; `comparison_problems` refuses any other pair before a run
starts). A switch can be one limit of the run budget (`budget.max_tool_calls`). Their runs
are paired by scenario and seed, so each scenario is compared only with itself:

    outcomes      both recovered, only the baseline, only the variant, neither
    diagnosis     the correct root service and mechanism, on each side
    differences   per pair, variant minus baseline: tool calls, decisions, estimated
                  tokens, rounds, specialists, context items dropped, claims, seconds
    integrity     recorded provenance that differs beyond the switch (there should be
                  none: a difference there means the pair measured something else too),
                  down to the single budget or context limit a switch may change

A batch keeps its comparisons' results as measured when it was made (EvalBatch.
comparison_results), so a later change here never rewrites a published batch. Nothing is
pooled across configurations and nothing claims significance: N is a handful of simulated
scenarios, and in a scripted batch the decisions come from a scripted policy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

from evals.records import (
    Comparison,
    ComparisonResult,
    EvalBatch,
    EvalRecord,
    MetricDifference,
    PairedOutcome,
    SetCount,
)
from recap.core.models import Contract, RunBudget, RunConfig

# The provenance two paired runs must share.
PROVENANCE = (
    "strategy",
    "retrieval_mode",
    "memory",
    "memory_read",
    "memory_write",
    "budget_limits",
    "context_limits",
    "safety_enabled",
    "planner",
    "provider",
    "model",
    "prompt_version",
    "code_version",
    "config_hash",
    "fixture_hash",
    "router",
    "verifier",
    "policy_version",
    "seed",
    "target",
)
# The recorded fields each switch may change: None, the whole field; a set, only those keys
# of a dict field (the other limits must be equal). `budget.<limit>` allows only that limit.
SWITCH_FIELDS: dict[str, dict[str, frozenset[str] | None]] = {
    "strategy": {"strategy": None},
    "use_rag": {"retrieval_mode": None},
    "retrieval": {"retrieval_mode": None},
    "use_memory": {"memory": None},
    "budget": {"budget_limits": None},  # the whole budget: prefer budget.<limit>
    "pack_tokens": {"context_limits": frozenset({"pack_tokens"})},
    "max_item_tokens": {"context_limits": frozenset({"max_item_tokens"})},
    "safety_enabled": {"safety_enabled": None},
    # The red-team planner wraps the provider, whose name then says so.
    "planner": {"planner": None, "provider": None},
}
# What is compared pair by pair (variant minus baseline), in this order. `seconds` is wall
# time: it includes starting the tool server and the simulated tool latency.
METRICS = (
    "tool_calls",
    "steps",
    "estimated_tokens",
    "rounds",
    "specialists",
    "retrieved_docs",
    "memory_hits",
    "context_dropped",
    "context_compacted",
    "claims_supported",
    "claims_insufficient",
    "disagreements",
    "unsafe_blocked",
    "unsafe_executed",
    "seconds",
)
REDTEAM = "redteam"  # the suite, and the scenario set, of the compromised-planner runs


class ComparisonPlan(Contract):
    """A file of comparisons (evals/comparisons/<name>.yaml)."""

    name: str
    comparisons: list[Comparison]


# --- planning -------------------------------------------------------------------------------


def load_comparisons(path: Path) -> ComparisonPlan:
    return ComparisonPlan.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def allowed_differences(switch: str) -> dict[str, frozenset[str] | None] | None:
    """The recorded fields a switch may change (None: not a switch a comparison can measure)."""
    top, _, limit = switch.partition(".")
    if not limit:
        return SWITCH_FIELDS.get(top)
    if top == "budget" and limit in RunBudget.model_fields:
        return {"budget_limits": frozenset({limit})}
    return None


def effective_switches(switches: Mapping[str, Any]) -> dict[str, Any]:
    """A configuration's switches over RunConfig's defaults: what its runs are given before
    the runner's shared settings (memory, approvals, label), which are the same for all."""
    return RunConfig.model_validate(dict(switches)).model_dump(mode="json", exclude={"label"})


def differing_switches(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    """The RunConfig fields two configurations set differently, once defaults are applied."""
    left, right = effective_switches(a), effective_switches(b)
    return sorted(key for key in left if left[key] != right[key])


def comparison_problems(
    comparisons: Sequence[Comparison],
    configs: Mapping[str, tuple[str, Mapping[str, Any]]],
    default_budget: Mapping[str, Any] | None = None,
) -> list[str]:
    """Why a comparison would not measure its one switch ([] when every one does).

    `configs` maps each configuration's name to its suite and switches. A pair must name two
    known configurations of one suite that differ in exactly the declared switch. For
    `budget.<limit>`, the two budgets (a configuration without one runs `default_budget`,
    when given) must also differ in that limit only; the runs' records are checked again.
    """
    problems: list[str] = []
    names = [c.name for c in comparisons]
    problems += [
        f"comparison {n!r} is named twice" for n in sorted(set(names)) if names.count(n) > 1
    ]
    for c in comparisons:
        missing = [name for name in (c.baseline, c.variant) if name not in configs]
        if missing:
            problems.append(f"{c.name}: unknown configuration(s) {', '.join(missing)}")
            continue
        (suite_a, a), (suite_b, b) = configs[c.baseline], configs[c.variant]
        if suite_a != suite_b:
            problems.append(
                f"{c.name}: {c.baseline} runs the {suite_a} suite, {c.variant} {suite_b}"
            )
        if allowed_differences(c.switch) is None:
            problems.append(f"{c.name}: {c.switch!r} is not a switch a comparison can measure")
            continue
        top, _, limit = c.switch.partition(".")
        differ = differing_switches(a, b)
        if differ != [top]:
            shown = ", ".join(differ) or "nothing"
            problems.append(f"{c.name}: the two differ in {shown}, not only in {c.switch}")
        elif limit:
            budgets = [_budget(cfg, default_budget) for cfg in (a, b)]
            if None not in budgets:
                left, right = budgets
                assert left is not None and right is not None
                other = sorted(k for k in left if k != limit and left[k] != right[k])
                if other or left[limit] == right[limit]:
                    shown = ", ".join(other) or "nothing"
                    problems.append(f"{c.name}: the budgets differ in {shown}, not only in {limit}")
    return problems


def _budget(
    switches: Mapping[str, Any], default: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """A configuration's run budget, if it can be known before the runs."""
    given = switches.get("budget")
    if given is not None:
        return RunBudget.model_validate(given).model_dump(mode="json")
    return RunBudget.model_validate(dict(default)).model_dump(mode="json") if default else None


# --- measuring ------------------------------------------------------------------------------


def compare(batch: EvalBatch, comparison: Comparison) -> ComparisonResult:
    """Pair the two configurations' runs of `batch` by scenario and seed, and measure."""
    base = _by_case(r for r in batch.records if r.label == comparison.baseline)
    other = _by_case(r for r in batch.records if r.label == comparison.variant)
    keys = [key for key in base if key in other]
    unpaired = sorted(_shown_case(key) for key in set(base) ^ set(other))
    sets = _scenario_sets(batch, _suite_of(batch, comparison.baseline))
    allowed = allowed_differences(comparison.switch) or {}
    pairs: list[PairedOutcome] = []
    unexpected: list[str] = []
    for key in keys:
        a, b = base[key], other[key]
        pairs.append(
            PairedOutcome(
                scenario_id=a.scenario_id,
                scenario_set=sets.get(a.scenario_id, ""),
                seed=a.seed,
                baseline=_outcome(a),
                variant=_outcome(b),
                baseline_diagnosis_correct=a.diagnosis_correct,
                variant_diagnosis_correct=b.diagnosis_correct,
                baseline_run=a.run_id,
                variant_run=b.run_id,
            )
        )
        for name in PROVENANCE:
            left, right = getattr(a, name), getattr(b, name)
            if name in allowed:
                keys_allowed = allowed[name]
                if keys_allowed is None:
                    continue
                left = {k: v for k, v in (left or {}).items() if k not in keys_allowed}
                right = {k: v for k, v in (right or {}).items() if k not in keys_allowed}
            if left != right:
                unexpected.append(f"{name}: {_shown_case(key)} ({left} vs {right})")
    by_set: dict[str, SetCount] = {}
    for name in dict.fromkeys(p.scenario_set for p in pairs):
        members = [p for p in pairs if p.scenario_set == name]
        by_set[name or "unlabelled"] = SetCount(
            n=len(members),
            baseline_successes=sum(p.baseline == "success" for p in members),
            variant_successes=sum(p.variant == "success" for p in members),
        )
    return ComparisonResult(
        comparison=comparison,
        n=len(pairs),
        unpaired=unpaired,
        baseline_successes=sum(p.baseline == "success" for p in pairs),
        variant_successes=sum(p.variant == "success" for p in pairs),
        both_succeeded=sum(p.baseline == p.variant == "success" for p in pairs),
        neither=sum("success" not in (p.baseline, p.variant) for p in pairs),
        only_baseline=[p.scenario_id for p in pairs if p.baseline == "success" != p.variant],
        only_variant=[p.scenario_id for p in pairs if p.variant == "success" != p.baseline],
        baseline_correct=sum(p.baseline_diagnosis_correct is True for p in pairs),
        variant_correct=sum(p.variant_diagnosis_correct is True for p in pairs),
        by_set=by_set,
        pairs=pairs,
        differences=[_difference(m, [(base[k], other[k]) for k in keys]) for m in METRICS],
        unexpected=unexpected,
    )


def compare_all(batch: EvalBatch) -> list[ComparisonResult]:
    return [compare(batch, comparison) for comparison in batch.comparisons]


def results_of(batch: EvalBatch) -> list[ComparisonResult]:
    """A batch's comparisons as measured when it was made; measured now for a batch that
    planned comparisons before results were kept with it."""
    return list(batch.comparison_results) or compare_all(batch)


def _suite_of(batch: EvalBatch, name: str) -> str:
    """A configuration's suite, as the batch names it (else from its runs' planner)."""
    for config in batch.configurations:
        if config.name == name:
            return config.suite
    runs = [r for r in batch.records if r.label == name]
    return REDTEAM if runs and all(r.planner == "prompt_injected" for r in runs) else "test"


def _scenario_sets(batch: EvalBatch, suite: str) -> dict[str, str]:
    """Each scenario's set for a comparison of `suite`: a scenario may be in the test set and
    in the red-team set (V1's and W4's injection scenario is), but a test-suite run of it
    belongs to the test set and a red-team run to the red-team set. The first set wins."""
    found: dict[str, str] = {}
    for name, ids in batch.scenario_sets.items():
        if (name == REDTEAM) != (suite == REDTEAM):
            continue
        for sid in ids:
            found.setdefault(sid, name)
    return found


def _by_case(records: Iterable[EvalRecord]) -> dict[tuple[str, int | None], EvalRecord]:
    """A configuration's runs by (scenario, seed). A repeated case keeps its first run."""
    found: dict[tuple[str, int | None], EvalRecord] = {}
    for record in records:
        found.setdefault((record.scenario_id, record.seed), record)
    return found


def _shown_case(key: tuple[str, int | None]) -> str:
    scenario, seed = key
    return scenario if seed is None else f"{scenario}@{seed}"


def _outcome(record: EvalRecord) -> str:
    return "success" if record.success else (record.failure or "error")


def _value(record: EvalRecord, metric: str) -> float | None:
    value = getattr(record, metric)
    if isinstance(value, list):  # retrieved_docs, memory_hits: how many
        return float(len(value))
    if isinstance(value, bool) or value is None:
        return None
    return float(value)


def _difference(metric: str, pairs: Sequence[tuple[EvalRecord, EvalRecord]]) -> MetricDifference:
    both = [
        (left, right)
        for left, right in ((_value(a, metric), _value(b, metric)) for a, b in pairs)
        if left is not None and right is not None
    ]
    if not both:
        return MetricDifference(metric=metric, n=0)
    return MetricDifference(
        metric=metric,
        n=len(both),
        baseline_mean=round(mean(left for left, _ in both), 2),
        variant_mean=round(mean(right for _, right in both), 2),
        mean_difference=round(mean(right - left for left, right in both), 2),
        changed=sum(left != right for left, right in both),
    )


# --- reading --------------------------------------------------------------------------------


def comparisons_markdown(results: Sequence[ComparisonResult]) -> list[str]:
    """The comparisons section of a batch's digest (empty for a batch without any)."""
    if not results:
        return []
    lines = [
        "## Comparisons (paired by scenario; one switch each)",
        "",
        "| comparison | switch | N pairs | baseline recovered | variant recovered | only "
        "baseline | only variant | correct diagnosis (baseline / variant) | provenance beyond "
        "the switch |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        c = r.comparison
        lines.append(
            f"| {c.name}: `{c.baseline}` vs `{c.variant}` | {c.switch} | {r.n} | "
            f"{r.baseline_successes} of {r.n} | {r.variant_successes} of {r.n} | "
            f"{counted(r.only_baseline) or '-'} | {counted(r.only_variant) or '-'} | "
            f"{r.baseline_correct} / {r.variant_correct} | "
            f"{'none' if not r.unexpected else f'{len(r.unexpected)} difference(s)'} |"
        )
    lines += ["", "By scenario set (recovered, baseline → variant):", ""]
    for r in results:
        sets = "; ".join(
            f"{name} {count.baseline_successes} of {count.n} → {count.variant_successes} of "
            f"{count.n}"
            for name, count in r.by_set.items()
        )
        lines.append(f"- {r.comparison.name}: {sets or 'no pair'}.")
    lines += ["", "Mean per-pair differences (variant minus baseline; pairs that changed):", ""]
    for r in results:
        moved = [d for d in r.differences if d.n and d.changed]
        shown = "; ".join(
            f"{d.metric} {_signed(d.mean_difference)} ({d.changed} of {d.n} changed)" for d in moved
        )
        lines.append(f"- {r.comparison.name}: {shown or 'no measured difference'}.")
    flagged = [r for r in results if r.unexpected or r.unpaired]
    for r in flagged:
        lines.append(
            f"- {r.comparison.name} integrity: "
            + "; ".join([*r.unexpected, *(f"unpaired {u}" for u in r.unpaired)])
        )
    return lines + [""]


def counted(scenarios: Sequence[str]) -> str:
    """Scenario ids once each, a repeat (one per seed) as a count: "a ×3, b"."""
    counts: dict[str, int] = {}
    for sid in scenarios:
        counts[sid] = counts.get(sid, 0) + 1
    return ", ".join(sid if n == 1 else f"{sid} ×{n}" for sid, n in counts.items())


def _signed(value: float | None) -> str:
    if value is None:
        return "-"
    return f"+{value:g}" if value > 0 else f"{value:g}"
