"""One-switch comparisons (M6 W4): a pair must differ in exactly its switch, its runs are paired
by scenario and seed, and anything else that differs between two paired runs is reported."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.compare import (
    compare,
    compare_all,
    comparison_problems,
    comparisons_markdown,
    differing_switches,
    load_comparisons,
    results_of,
)
from evals.records import Comparison, EvalConfiguration, EvalRecord, make_batch
from evals.runner import EVALS_DIR, load_eval_configs, load_split
from recap.core.ids import utcnow

PAIR = Comparison(
    name="single-vs-parallel",
    baseline="single_only",
    variant="parallel_only",
    switch="strategy",
    question="q",
)


def record(label: str, scenario: str, *, success: bool = True, **fields: Any) -> EvalRecord:
    return EvalRecord(
        run_id=f"run-{label}-{scenario}-{fields.get('seed')}",
        mode="scripted_regression",
        scenario_id=scenario,
        label=label,
        success=success,
        failure="" if success else fields.pop("failure", "oracle_not_recovered"),
        strategy=fields.pop("strategy", "single" if label == "single_only" else "parallel"),
        **fields,
    )


def batch(records: list[EvalRecord], sets: dict[str, list[str]] | None = None) -> Any:
    return make_batch(
        batch_id="b",
        mode="scripted_regression",
        title="t",
        records=records,
        created_at=utcnow(),
        comparisons=[PAIR],
        scenario_sets=sets or {},
    )


def test_two_configurations_differ_in_the_switches_they_set_once_defaults_apply() -> None:
    assert differing_switches({"strategy": "single"}, {"strategy": "parallel"}) == ["strategy"]
    # Spelling a default out is no difference.
    assert differing_switches({"use_rag": True}, {}) == []
    assert differing_switches({}, {"use_rag": False, "use_memory": False}) == [
        "use_memory",
        "use_rag",
    ]


def test_a_pair_that_differs_in_more_than_its_switch_is_refused() -> None:
    configs = {
        "a": ("test", {"strategy": "single"}),
        "b": ("test", {"strategy": "parallel", "use_rag": False}),
        "c": ("redteam", {"strategy": "parallel"}),
        "d": ("test", {"strategy": "single"}),
    }

    def problems(baseline: str, variant: str, switch: str = "strategy") -> list[str]:
        pair = Comparison(name="x", baseline=baseline, variant=variant, switch=switch, question="")
        return comparison_problems([pair], configs)

    assert "differ in strategy, use_rag, not only in strategy" in problems("a", "b")[0]
    assert any("runs the test suite" in p for p in problems("a", "c"))
    assert "differ in nothing" in problems("a", "d")[0]
    assert "unknown configuration" in problems("a", "zz")[0]
    assert "not a switch" in problems("a", "b", "label")[0]
    twice = Comparison(name="x", baseline="a", variant="d", switch="strategy", question="")
    assert any("named twice" in p for p in comparison_problems([twice, twice], configs))


def test_the_committed_w4_comparisons_each_differ_in_exactly_their_switch() -> None:
    plan = load_comparisons(EVALS_DIR / "comparisons" / "w4.yaml")
    names = load_split(EVALS_DIR / "splits" / "w4.yaml").configurations
    configs = {c.name: (c.suite, c.config) for c in load_eval_configs(names)}
    default = {"max_steps": 30, "max_tool_calls": 30, "max_elapsed_seconds": 120.0}
    default |= {
        "max_estimated_tokens": 120000,
        "max_parallel_agents": 2,
        "max_model_tokens": 200000,
    }
    assert comparison_problems(plan.comparisons, configs, default) == []
    assert len(plan.comparisons) == 9
    # Every comparison names what it measures and what its result would change.
    assert all(c.question and c.decision for c in plan.comparisons)


def test_runs_are_paired_by_scenario_and_seed_and_outcomes_counted() -> None:
    records = [
        record("single_only", "s1"),
        record("parallel_only", "s1"),
        record("single_only", "s2", success=False),
        record("parallel_only", "s2"),
        record("single_only", "s3"),
        record("parallel_only", "s3", success=False, failure="budget_exhausted"),
        record("single_only", "s4"),  # no partner: unpaired
    ]
    result = compare(batch(records, {"dev": ["s1", "s2"], "held_out": ["s3"]}), PAIR)
    assert result.n == 3
    assert (result.baseline_successes, result.variant_successes) == (2, 2)
    assert (result.both_succeeded, result.neither) == (1, 0)
    assert result.only_baseline == ["s3"] and result.only_variant == ["s2"]
    assert result.unpaired == ["s4"]
    assert [p.variant for p in result.pairs] == ["success", "success", "budget_exhausted"]
    assert result.by_set["dev"].n == 2 and result.by_set["held_out"].variant_successes == 0
    assert result.unexpected == []  # only the strategy differs, which is the switch


def test_metric_differences_are_variant_minus_baseline_over_pairs_that_recorded_both() -> None:
    records = [
        record("single_only", "s1", tool_calls=12, estimated_tokens=1000, context_dropped=0),
        record("parallel_only", "s1", tool_calls=9, estimated_tokens=900, context_dropped=2),
        record("single_only", "s2", tool_calls=10, context_dropped=None),
        record("parallel_only", "s2", tool_calls=10, context_dropped=3),
    ]
    differences = {d.metric: d for d in compare(batch(records), PAIR).differences}
    tools = differences["tool_calls"]
    assert (tools.n, tools.baseline_mean, tools.variant_mean) == (2, 11.0, 9.5)
    assert (tools.mean_difference, tools.changed) == (-1.5, 1)
    dropped = differences["context_dropped"]
    assert (dropped.n, dropped.mean_difference) == (1, 2.0)  # s2's baseline did not record it
    assert differences["estimated_tokens"].mean_difference == -50.0


def test_provenance_that_differs_beyond_the_switch_is_reported() -> None:
    records = [
        record("single_only", "s1", prompt_version="v1", seed=7),
        record("parallel_only", "s1", prompt_version="v2", seed=7),
    ]
    result = compare(batch(records), PAIR)
    assert result.unexpected == ["prompt_version: s1@7 (v1 vs v2)"]
    markdown = "\n".join(comparisons_markdown([result]))
    assert "1 difference(s)" in markdown and "prompt_version" in markdown


def test_another_seed_is_another_case_never_paired_with_the_first() -> None:
    records = [
        record("single_only", "s1", seed=1),
        record("parallel_only", "s1", seed=2),
    ]
    result = compare(batch(records), PAIR)
    assert result.n == 0 and result.unpaired == ["s1@1", "s1@2"]


def test_a_batch_without_comparisons_has_no_comparison_section() -> None:
    assert comparisons_markdown([]) == []


def test_batches_name_their_configurations_verbatim(tmp_path: Path) -> None:
    configuration = EvalConfiguration(name="tight", suite="test", switches={"budget": {"x": 1}})
    made = make_batch(
        batch_id="b",
        mode="scripted_regression",
        title="t",
        records=[],
        created_at=utcnow(),
        configurations=[configuration],
    )
    assert made.configurations[0].switches == {"budget": {"x": 1}}


def test_a_scenario_in_the_test_and_red_team_sets_belongs_to_the_suite_that_ran_it() -> None:
    """V1's and W4's injection scenario is in both sets: its test-suite run is a test case."""
    records = [
        record("single_only", "inject"),
        record("parallel_only", "inject"),
        record("single_only", "s2"),
        record("parallel_only", "s2"),
        record(
            "red_off",
            "inject",
            planner="prompt_injected",
            safety_enabled=False,
            strategy="adaptive",
        ),
        record(
            "red_on", "inject", planner="prompt_injected", safety_enabled=True, strategy="adaptive"
        ),
    ]
    made = batch(records, {"test": ["inject", "s2"], "redteam": ["inject"]})
    test_suite = compare(made, PAIR)
    assert set(test_suite.by_set) == {"test"} and test_suite.by_set["test"].n == 2
    assert {p.scenario_set for p in test_suite.pairs} == {"test"}
    safety = Comparison(
        name="s", baseline="red_off", variant="red_on", switch="safety_enabled", question=""
    )
    red = compare(made, safety)  # no configurations listed: the planner tells the suite
    assert set(red.by_set) == {"redteam"} and red.unexpected == []


def test_a_budget_limit_switch_allows_that_limit_only() -> None:
    limits = {"max_steps": 30, "max_tool_calls": 30, "max_elapsed_seconds": 120.0}
    tight = dict(limits, max_tool_calls=15)
    configs = {
        "full": ("test", {}),
        "tight": ("test", {"budget": tight}),
        "tight_and_slow": ("test", {"budget": dict(tight, max_elapsed_seconds=300.0)}),
    }
    pair = Comparison(
        name="t", baseline="full", variant="tight", switch="budget.max_tool_calls", question=""
    )
    both = pair.model_copy(update={"variant": "tight_and_slow"})
    assert comparison_problems([pair], configs, limits) == []
    assert (
        "budgets differ in max_elapsed_seconds" in comparison_problems([both], configs, limits)[0]
    )
    bad = pair.model_copy(update={"switch": "budget.not_a_limit"})
    assert "not a switch" in comparison_problems([bad], configs, limits)[0]
    # From the records: the allowed limit may differ, any other limit is reported.
    base = record("single_only", "s1", strategy="adaptive", budget_limits=limits)
    ok = record("parallel_only", "s1", strategy="adaptive", budget_limits=tight)
    sneaky = record(
        "parallel_only", "s1", strategy="adaptive", budget_limits=dict(tight, max_steps=40)
    )
    by_limit = PAIR.model_copy(update={"switch": "budget.max_tool_calls"})
    assert compare(batch([base, ok]), by_limit).unexpected == []
    [flag] = compare(batch([base, sneaky]), by_limit).unexpected
    assert flag.startswith("budget_limits: s1") and "max_steps" in flag


def test_a_context_switch_allows_its_own_limit_only() -> None:
    pack = PAIR.model_copy(update={"switch": "pack_tokens"})
    base = record(
        "single_only",
        "s1",
        strategy="adaptive",
        context_limits={"pack_tokens": 3000, "max_item_tokens": 400},
    )
    smaller = record(
        "parallel_only",
        "s1",
        strategy="adaptive",
        context_limits={"pack_tokens": 1500, "max_item_tokens": 400},
    )
    both = record(
        "parallel_only",
        "s1",
        strategy="adaptive",
        context_limits={"pack_tokens": 1500, "max_item_tokens": 32},
    )
    assert compare(batch([base, smaller]), pack).unexpected == []
    assert len(compare(batch([base, both]), pack).unexpected) == 1


def test_a_batch_keeps_its_comparisons_as_measured_when_it_was_made() -> None:
    made = batch([record("single_only", "s1"), record("parallel_only", "s1", success=False)])
    kept = made.model_copy(update={"comparison_results": compare_all(made)})
    assert results_of(kept) == kept.comparison_results
    # Later records (or a later compare.py) never rewrite what was kept.
    changed = kept.model_copy(
        update={"records": [record("single_only", "s1"), record("parallel_only", "s1")]}
    )
    assert results_of(changed)[0].variant_successes == 0
    assert results_of(made)[0].variant_successes == 0  # measured now when nothing was kept


def test_a_disagreement_that_stays_open_over_rounds_counts_once() -> None:
    """Each round's report is rebuilt from all of the epoch's claims, so an open disagreement
    (or one settled in a later round) appears in every round: it is one disagreement."""
    from evals.categorize import _distinct_disagreements

    conflict = {
        "kind": "competing_root_causes",
        "claims": ["b on cart", "a on cart"],
        "resolved": False,
    }
    settled = dict(conflict, claims=["a on cart", "b on cart"], resolved=True)
    other = {"kind": "detail_conflict", "claims": ["a on cart"], "resolved": False}
    rounds = [{"disagreements": [conflict]}, {"disagreements": [settled, other]}, {"checks": []}]
    assert len(_distinct_disagreements(rounds)) == 2
