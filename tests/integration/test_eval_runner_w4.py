"""The evaluation runner's M6 W4 additions: held-out scenarios, one-switch comparisons, hybrid
retrieval from stored query vectors, and the refusals that keep every scored run comparable.

One small evaluation runs once for this module (the train phase, then three configurations
on one test and one held-out scenario), end to end through the real MCP server.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from evals import runner
from evals.categorize import run_events
from evals.datasets import alert_query, scenario_files
from evals.records import EvalBatch, load_batch
from evals.runner import (
    EvalConfig,
    EvalReport,
    Split,
    _check_split,
    _eval_settings,
    _scored_config,
    run_evaluation,
)
from recap.core.config import REPO_ROOT
from recap.core.errors import ConfigError
from recap.harness.store import RunStore
from recap.runtime import Runtime
from recap.telemetry.sinks import SqliteTraceSink
from recap.telemetry.tracer import TraceCategory

VECTORS = REPO_ROOT / "evals" / "datasets" / "query-vectors-v1.json"
TRAIN = ["train-checkout-bad-release", "train-cart-memory-leak", "train-payment-cascade"]
COMPARISONS = {
    "name": "mini",
    "comparisons": [
        {
            "name": "adaptive-vs-single",
            "baseline": "single_only",
            "variant": "full_adaptive",
            "switch": "strategy",
            "question": "q",
            "decision": "d",
        },
        {
            "name": "bm25-vs-hybrid",
            "baseline": "full_adaptive",
            "variant": "hybrid",
            "switch": "retrieval",
            "question": "q",
            "decision": "d",
        },
    ],
}


def quiet(message: str) -> None:
    """A progress callback that prints nothing."""


def write_yaml(path: Path, data: object) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def w4(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, EvalReport, EvalBatch]:
    root = tmp_path_factory.mktemp("w4")
    split = write_yaml(
        root / "split.yaml",
        {
            "name": "mini",
            "train": TRAIN,
            "test": ["test-cart-memory-leak"],
            "held_out": ["m3-inventory-leak-behind-frontend"],
            # Also a red-team scenario, as V1's and W4's injection scenario is: its test-suite
            # runs must still count in the test set.
            "redteam": ["test-cart-memory-leak"],
        },
    )
    comparisons = write_yaml(root / "comparisons.yaml", COMPARISONS)
    out_dir = root / "report"
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("RECAP_PROVIDER", raising=False)
        patch.setenv("RECAP_TARGET", "kind-demo")  # ignored: an evaluation runs on the simulator
        report = asyncio.run(
            run_evaluation(
                out_dir,
                config_names=["full_adaptive", "single_only", "hybrid"],
                read_latency_ms=0,
                write_latency_ms=0,
                progress=quiet,
                split_path=split,
                comparisons_path=comparisons,
                query_vectors_path=VECTORS,
            )
        )
    return out_dir, report, load_batch(out_dir / "batch.json")


@pytest.mark.e2e
def test_held_out_scenarios_are_scored_with_the_test_suite_and_named_as_such(
    w4: tuple[Path, EvalReport, EvalBatch],
) -> None:
    _, report, batch = w4
    assert [c.scenario_id for c in report.cases] == [
        "test-cart-memory-leak",
        "m3-inventory-leak-behind-frontend",
    ] * 3
    assert batch.scenario_sets == {
        "test": ["test-cart-memory-leak"],
        "held_out": ["m3-inventory-leak-behind-frontend"],
        "redteam": ["test-cart-memory-leak"],
    }
    # V1's configurations first, then W4's (evals/ablations/w4/), each in file-name order.
    assert [c.name for c in batch.configurations] == ["full_adaptive", "single_only", "hybrid"]
    hybrid = next(c for c in batch.configurations if c.name == "hybrid")
    assert hybrid.switches["retrieval"] == "hybrid"  # verbatim from its file
    assert report.query_vectors == "evals/datasets/query-vectors-v1.json"


@pytest.mark.e2e
def test_hybrid_runs_answer_every_query_from_the_stored_vectors(
    w4: tuple[Path, EvalReport, EvalBatch],
) -> None:
    out_dir, _, batch = w4
    runs = RunStore(out_dir / "data" / "recap.db")
    hybrid = [r for r in batch.records if r.label == "hybrid"]
    assert hybrid and {r.retrieval_mode for r in hybrid} == {"hybrid"}
    for record in hybrid:
        run = runs.get(record.run_id)
        assert run is not None and run.retrieval is not None
        info = run.retrieval
        # Every embedding came from the stored file: no request reached a model server.
        assert info.embedding_requests > 0 and info.replayed_requests == info.embedding_requests
        assert info.embedder == "ollama:nomic-embed-text" and (info.server or {}).get("stored")
        assert record.success


@pytest.mark.e2e
def test_the_comparisons_are_measured_and_differ_only_in_their_switch(
    w4: tuple[Path, EvalReport, EvalBatch],
) -> None:
    out_dir, _, batch = w4
    from evals.compare import compare_all

    results = {r.comparison.name: r for r in compare_all(batch)}
    assert set(results) == {"adaptive-vs-single", "bm25-vs-hybrid"}
    assert batch.comparison_results == list(results.values())  # kept with the batch
    for result in results.values():
        assert result.n == 2 and result.unpaired == [] and result.unexpected == []
        assert set(result.by_set) == {"test", "held_out"}  # never "redteam" for this suite
        assert {p.scenario_set for p in result.pairs} == {"test", "held_out"}
    readme = (out_dir / "batch.json").read_text(encoding="utf-8")
    assert json.loads(readme)["comparisons"][0]["switch"] == "strategy"


@pytest.mark.e2e
def test_every_record_names_its_memory_limits_seed_and_routing(
    w4: tuple[Path, EvalReport, EvalBatch],
) -> None:
    _, _, batch = w4
    for record in batch.records:
        assert record.memory_read == ["train"] and record.memory_write is None
        assert record.seed is not None and record.fixture_hash and record.router
        assert record.budget_limits["max_tool_calls"] == 30
        assert record.context_limits == {"pack_tokens": 3000, "max_item_tokens": 400}
        assert record.routing_rules and record.specialists
        assert record.claims_supported is not None and record.unsafe_executed == 0
        assert record.action_sources and record.wrong_target_writes == 0
        assert all(hit["source_scenario"] in TRAIN for hit in record.memory_hits)


@pytest.mark.e2e
def test_the_retrieval_dataset_alert_query_is_what_the_controller_searched(
    w4: tuple[Path, EvalReport, EvalBatch],
) -> None:
    out_dir, _, batch = w4
    events = SqliteTraceSink(out_dir / "data" / "recap.db")
    scenarios = scenario_files()
    for record in batch.records:
        trace = run_events(events, record.run_id)
        first = next(
            e
            for e in trace
            if e.category == TraceCategory.RETRIEVAL and e.payload["stage"] == "initial"
        )
        assert first.payload["query"] == alert_query(scenarios[record.scenario_id])


# --- refusals, before anything runs ---------------------------------------------------------


@pytest.mark.parametrize(
    "switches",
    [
        {"provider": "ollama"},
        {"replay_fixture": "x"},
        {"approval_mode": "manual"},
        {"label": "other"},
        {"tool_latency_ms": 0},
    ],
)
def test_a_configuration_cannot_set_what_the_runner_owns(switches: dict[str, object]) -> None:
    config = EvalConfig(name="cheat", description="d", suite="test", config=switches)
    with pytest.raises(ConfigError, match="the runner sets"):
        _scored_config(config)


async def test_a_comparison_that_differs_in_more_than_its_switch_is_refused(tmp_path: Path) -> None:
    bad = dict(COMPARISONS)
    bad["comparisons"] = [
        {
            "name": "x",
            "baseline": "baseline_single",
            "variant": "full_adaptive",
            "switch": "strategy",
            "question": "q",
        },
    ]
    with pytest.raises(ConfigError, match="not only in strategy"):
        await run_evaluation(
            tmp_path / "report",
            config_names=["baseline_single", "full_adaptive"],
            comparisons_path=write_yaml(tmp_path / "c.yaml", bad),
            progress=quiet,
        )
    assert not (tmp_path / "report").exists()


async def test_hybrid_without_stored_query_vectors_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="query vectors"):
        await run_evaluation(tmp_path / "report", config_names=["hybrid"], progress=quiet)
    with pytest.raises(ConfigError, match="no stored query vectors"):
        await run_evaluation(
            tmp_path / "report",
            config_names=["hybrid"],
            query_vectors_path=tmp_path / "missing.json",
            progress=quiet,
        )
    assert not (tmp_path / "report").exists()


def test_the_evaluation_runs_on_the_simulator_whatever_recap_target_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECAP_TARGET", "kind-demo")
    settings = _eval_settings(tmp_path, 0, 0)
    assert settings.target is None
    assert Runtime(settings).environment_info().is_simulated


def test_held_out_scenarios_must_be_test_scenarios_apart_from_the_others() -> None:
    from evals.runner import load_split

    scenarios = {s.id: s for s in Runtime().list_scenarios()}
    base = load_split()
    overlapping = Split(name="x", train=base.train, test=base.test, held_out=base.test[:1])
    with pytest.raises(ValueError, match="held-out"):
        _check_split(overlapping, scenarios)
    labelled_train = Split(name="x", train=base.train[1:], test=base.test, held_out=base.train[:1])
    with pytest.raises(ValueError, match="labeled"):
        _check_split(labelled_train, scenarios)
    with pytest.raises(ValueError, match="unknown"):
        _check_split(Split(name="x", train=[], test=["nope"]), scenarios)
    with pytest.raises(ValueError, match="unknown scenarios: train-typo"):  # not a KeyError
        _check_split(Split(name="x", train=["train-typo"], test=base.test), scenarios)


async def test_an_evaluation_needs_a_fresh_data_folder(tmp_path: Path) -> None:
    out = tmp_path / "report"
    (out / "data").mkdir(parents=True)
    with pytest.raises(ConfigError, match="fresh folder"):
        await run_evaluation(out, config_names=["baseline_single"], progress=quiet)


async def test_stored_vectors_that_do_not_fit_or_cover_the_runs_are_refused_first(
    tmp_path: Path,
) -> None:
    from evals.query_vectors import QueryVectors
    from recap.core.config import load_settings
    from recap.knowledge.vectors import VectorIndex

    spec = load_settings(target="").retrieval.embedder
    stored = QueryVectors.load(VECTORS, spec)
    split = write_yaml(
        tmp_path / "split.yaml",
        {"name": "x", "train": TRAIN, "test": ["test-cart-memory-leak"]},
    )
    # Another model's weights: hybrid search would compare two models' vectors.
    other = VectorIndex.load(VECTORS)
    other.meta["weights"] = "sha256:another"
    other.save(tmp_path / "other.json")
    with pytest.raises(ConfigError, match="do not fit"):
        await run_evaluation(
            tmp_path / "a",
            config_names=["hybrid"],
            split_path=split,
            query_vectors_path=tmp_path / "other.json",
            progress=quiet,
        )
    # The right weights, but not the alert this split's hybrid run will search with.
    query = alert_query(scenario_files()["test-cart-memory-leak"])
    kept = {q: v for q in stored.index.meta["queries"] if q != query and (v := stored.lookup(q))}
    partial = VectorIndex(stored.index.embedder, stored.index.dimensions, spec.query_prefix, {}, {})
    partial.extended(kept, dict(stored.index.meta)).save(tmp_path / "partial.json")
    with pytest.raises(ConfigError, match="lack 1 of the hybrid runs' queries"):
        await run_evaluation(
            tmp_path / "b",
            config_names=["hybrid"],
            split_path=split,
            query_vectors_path=tmp_path / "partial.json",
            progress=quiet,
        )
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


async def test_a_configurations_own_seed_is_kept_unless_seeds_are_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RECAP_PROVIDER", raising=False)
    seeded = EvalConfig(name="seeded", description="d", suite="test", config={"seed": 7})
    monkeypatch.setattr(runner, "load_eval_configs", lambda names: [seeded])
    seen: list[int | None] = []

    async def no_run(runtime: object, scenario: object, config: object, name: str, suite: str):  # type: ignore[no-untyped-def]
        seen.append(config.seed)  # type: ignore[attr-defined]
        raise _Stop

    split = write_yaml(
        tmp_path / "split.yaml", {"name": "x", "train": [], "test": ["test-cart-memory-leak"]}
    )
    monkeypatch.setattr(runner, "_run_case", no_run)
    for out, seeds in (("a", None), ("b", [1])):
        with pytest.raises(_Stop):
            await run_evaluation(tmp_path / out, split_path=split, seeds=seeds, progress=quiet)
    assert seen == [7, 1]


class _Stop(Exception):
    """Ends a run_evaluation in a test right after the first scored case starts."""


def test_v1s_default_set_is_unchanged_and_w4s_configurations_load_by_name() -> None:
    from evals.runner import load_eval_configs

    assert [c.name for c in load_eval_configs()] == [
        "baseline_single",
        "full_adaptive",
        "no_memory",
        "no_rag",
        "redteam_safety_off",
        "redteam_safety_on",
        "single_only",
    ]
    assert [c.name for c in load_eval_configs(["tight_tools", "hybrid"])] == [
        "hybrid",
        "tight_tools",
    ]


def test_a_relative_path_is_recorded_from_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    assert runner._shown(Path("evals/splits/w4.yaml")) == "evals/splits/w4.yaml"
    assert runner._shown(Path("/tmp/elsewhere/report")) == "report"


@pytest.mark.e2e
async def test_a_capture_run_says_it_is_a_capture_not_a_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recap.knowledge.vectors import VectorIndex

    monkeypatch.delenv("RECAP_PROVIDER", raising=False)
    runbook = VectorIndex.load(
        REPO_ROOT / "fixtures" / "knowledge" / "vectors" / "nomic-embed-text.json"
    )

    class Live:
        name = "ollama:nomic-embed-text"
        server = {"weights": runbook.meta["weights"], "ollama_version": "test"}

        async def embed_query(self, text: str) -> list[float]:
            raise AssertionError("every query of this split is stored: nothing is embedded")

    monkeypatch.setattr(runner, "create_embedder", lambda settings, provider: Live())
    copy = tmp_path / "vectors.json"
    copy.write_bytes(VECTORS.read_bytes())
    split = write_yaml(
        tmp_path / "split.yaml", {"name": "x", "train": TRAIN, "test": ["test-cart-memory-leak"]}
    )
    report = await run_evaluation(
        tmp_path / "report",
        config_names=["hybrid"],
        split_path=split,
        query_vectors_path=copy,
        capture_query_vectors=True,
        read_latency_ms=0,
        write_latency_ms=0,
        progress=quiet,
    )
    assert report.query_vectors_captured
    methods = load_batch(tmp_path / "report" / "batch.json").methods
    assert any(line.startswith("A CAPTURE run") for line in methods)
    assert not any("no model server was contacted" in line for line in methods)


def test_a_rebuilt_batch_keeps_its_comparisons(
    w4: tuple[Path, EvalReport, EvalBatch], monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir, report, batch = w4
    # The fixture's comparisons file sits next to the report, recorded by its name.
    monkeypatch.setattr(runner, "REPO_ROOT", out_dir.parent)
    rebuilt = runner.rebuild_batch(out_dir)
    assert [c.name for c in rebuilt.comparisons] == [c.name for c in batch.comparisons]
    assert [r.n for r in rebuilt.comparison_results] == [r.n for r in batch.comparison_results]
