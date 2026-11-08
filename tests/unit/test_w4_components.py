"""Component evaluations (evals/components.py): each scorer counts what its definition says, on
small inputs whose answers are known, and the real datasets score offline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evals.components import (
    ComponentReport,
    component_report,
    evaluate_memory,
    evaluate_retrieval,
    evaluate_routing,
    evaluate_safety,
    find_reports,
    report_markdown,
    write_report,
)
from evals.datasets import LabelSet, MemoryPair, RetrievalDataset, RetrievalQuery, ScenarioLabel
from evals.query_vectors import QueryVectors
from evals.records import EvalConfiguration, EvalRecord, make_batch
from recap.core.config import REPO_ROOT, load_settings
from recap.core.ids import utcnow
from recap.core.models import DocChunk
from recap.knowledge.retriever import KnowledgeRetriever

SETTINGS = load_settings(target="")


def chunk(chunk_id: str, doc: str, text: str) -> DocChunk:
    return DocChunk(
        chunk_id=chunk_id, doc_path=doc, title=doc, heading=chunk_id, text=text, checksum="0" * 12
    )


CORPUS = [
    chunk("rollback#a", "rollback.md", "release rollback deployment version broke errors"),
    chunk("memory#a", "memory.md", "memory heap restart crash loop oom killed"),
    chunk("signals#a", "signals.md", "error rate latency dashboards"),
]
DATASET = RetrievalDataset(
    corpus="fixtures/knowledge",
    chunks=3,
    relevant={"bad_deployment": ["rollback#a"], "resource_pressure": ["memory#a"]},
    remediation={"bad_deployment": ["rollback#a"]},
    poisoned=[],
    not_scored=[],
    queries=[
        RetrievalQuery(
            id="q1",
            kind="paraphrase",
            text="the release broke",
            mechanism="bad_deployment",
            source="h",
        ),
        RetrievalQuery(
            id="q2",
            kind="paraphrase",
            text="oom killed again",
            mechanism="resource_pressure",
            source="h",
        ),
        RetrievalQuery(
            id="q3",
            kind="paraphrase",
            text="dashboards latency",
            mechanism="resource_pressure",
            source="h",
        ),
    ],
)


def test_retrieval_counts_hits_mrr_and_remediation_per_ranker() -> None:
    result = evaluate_retrieval(DATASET, KnowledgeRetriever(CORPUS), None)
    assert result.rankers == ["bm25", "bm25+rerank"]  # no vectors: dense and hybrid not measured
    assert result.not_measured == [
        "dense: no stored query vectors",
        "hybrid: no stored query vectors",
    ]
    score = next(s for s in result.scores if s.ranker == "bm25")
    assert (score.n, score.hit_at_1) == (3, 2)  # q3 ranks the signals guide first
    assert result.ranks["q1"]["bm25"] == 1 and result.ranks["q2"]["bm25"] == 1
    assert score.remediation_n == 1 and score.remediation_at_2 == 1
    assert score.mrr == round((1 + 1 + (1 / 2 if result.ranks["q3"]["bm25"] == 2 else 0)) / 3, 3)


def test_retrieval_with_vectors_measures_dense_and_hybrid() -> None:
    vectors = {"rollback#a": [1.0, 0.0], "memory#a": [0.0, 1.0], "signals#a": [0.6, 0.6]}
    index = KnowledgeRetriever(CORPUS, vectors=vectors)

    class Stub:
        def lookup(self, text: str) -> list[float]:
            return [1.0, 0.0] if "release" in text else [0.0, 1.0]

    result = evaluate_retrieval(DATASET, index, Stub())  # type: ignore[arg-type]
    assert result.rankers == ["bm25", "bm25+rerank", "dense", "hybrid"]
    dense = next(s for s in result.scores if s.ranker == "dense")
    assert dense.hit_at_1 == 3  # every query's vector points at its own runbook


def labels() -> LabelSet:
    def label(root: str) -> ScenarioLabel:
        return ScenarioLabel(
            partition="test",
            family="f",
            alert_service="cart",
            root_service=root,
            mechanism="resource_pressure",
            fix=f"restart_service on {root}",
            cross_service=root != "cart",
        )

    return LabelSet(
        scenarios={"s-twin": label("cart"), "s-other": label("payment"), "t1": label("cart")},
        memory_pairs=[
            MemoryPair(scenario="s-twin", remembered="t1", label="twin"),
            MemoryPair(scenario="s-other", remembered="t1", label="look_alike"),
        ],
    )


def record(scenario: str, label: str = "full_adaptive", **fields: Any) -> EvalRecord:
    return EvalRecord(
        run_id=f"run-{label}-{scenario}",
        mode="scripted_regression",
        scenario_id=scenario,
        label=label,
        **fields,
    )


def hit(source: str, safe: bool, partition: str = "train", assessed: bool = True) -> dict[str, Any]:
    return {
        "source_scenario": source,
        "partition": partition,
        "safe_to_reuse": safe,
        "similarity": 0.7,
        "assessed": assessed,
    }


def test_memory_verdicts_are_judged_against_the_labels_and_leakage_flagged() -> None:
    batch = make_batch(
        batch_id="b",
        mode="scripted_regression",
        title="t",
        created_at=utcnow(),
        records=[
            record("s-twin", memory=True, memory_hits=[hit("t1", True)], action_sources=["memory"]),
            record("s-other", memory=True, memory_hits=[hit("t1", True)]),  # unsafe reuse
            record("s-other", label="x", memory=True, memory_hits=[hit("s-twin", True, "test")]),
            # Stopped before its first verification: the twin was never judged, not missed.
            record(
                "s-twin", label="cut", memory=True, memory_hits=[hit("t1", False, assessed=False)]
            ),
            record("s-twin", label="off", memory=False),
        ],
    )
    result = evaluate_memory(batch, labels(), train=["t1"])
    assert (result.runs, result.hits, result.not_assessed) == (4, 4, 1)
    assert (result.twins, result.twins_accepted, result.false_unsafe) == (1, 1, 0)
    assert result.false_safe == 1  # the look-alike wrongly accepted
    assert result.unlabelled_safe == 1  # judged safe with no label: apart, and listed as leakage
    assert result.correct == 1  # of the two assessed, labelled hits
    assert result.by_label == {"look_alike": 1, "twin": 1, "unlabelled": 1}
    assert result.fix_from_memory == ["full_adaptive/s-twin"]
    assert result.leakage == ["run-x-s-other: s-twin (test)"]


def test_routing_counts_parallel_rounds_against_the_cross_service_label() -> None:
    batch = make_batch(
        batch_id="b",
        mode="scripted_regression",
        title="t",
        created_at=utcnow(),
        records=[
            record(
                "s-other",
                topology=["parallel: a + b"],
                specialists=2,
                supported_basis_by_agent={"a": 2, "b": 1},
                routing_rules=["RT2"],
            ),
            record(
                "s-twin", topology=["single: investigator"], specialists=1, routing_rules=["RT6"]
            ),
        ],
    )
    result = evaluate_routing(batch, labels())
    assert result.parallel_cross_service == [1, 1] and result.parallel_single_service == [0, 1]
    assert result.second_specialist_contributed == [1, 1]
    assert result.rules_fired == {"RT2": 1, "RT6": 1}


def test_safety_counts_false_blocks_on_legitimate_runs_only() -> None:
    batch = make_batch(
        batch_id="b",
        mode="scripted_regression",
        title="t",
        created_at=utcnow(),
        configurations=[
            EvalConfiguration(name="full_adaptive", suite="test"),
            EvalConfiguration(name="redteam_safety_on", suite="redteam"),
        ],
        records=[
            # Blocked today's exact fix: a false block. Blocked another fix: a right one.
            record(
                "s-twin",
                failure="safety_blocked",
                proposed_actions=["restart_service on cart [runbook]"],
            ),
            record(
                "s-other",
                failure="safety_blocked",
                proposed_actions=["restart_service on cart [memory]"],
            ),
            record("s2", success=True, wrong_target_writes=1),
            record(
                "s1",
                label="redteam_safety_on",
                failure="safety_blocked",
                unsafe_attempts=1,
                unsafe_blocked=1,
            ),
        ],
    )
    rows = {row.config: row for row in evaluate_safety(batch, labels())}
    full = rows["full_adaptive"]
    assert (full.false_blocks, full.blocked_other_fixes, full.wrong_target_writes) == (1, 1, 1)
    assert rows["redteam_safety_on"].false_blocks == 0  # its safe outcome is the block
    assert rows["redteam_safety_on"].unsafe_blocked == 1


def test_the_real_datasets_score_offline_with_the_stored_vectors(tmp_path: Path) -> None:
    path = REPO_ROOT / "evals" / "datasets" / "query-vectors-v1.json"
    vectors = QueryVectors.load(path, SETTINGS.retrieval.embedder)
    report = component_report(batch=None, vectors=vectors, vectors_path=path, settings=SETTINGS)
    assert report.retrieval is not None
    assert report.retrieval.rankers == ["bm25", "bm25+rerank", "dense", "hybrid"]
    assert report.retrieval.weights == vectors.weights
    # The red-team overlay is measured, and the remediation lookups always find their runbook.
    assert report.retrieval.exposure
    for score in report.retrieval.scores:
        if score.kind == "proposal":
            assert score.hit_at_1 == score.n
    assert report.memory is None and report.budget == []
    # The sample sizes are counted, never written in by hand.
    assert report.limitations[0].startswith("Small N: 17 alert queries, 2 remediation lookups")
    written = write_report(report, tmp_path / report.report_id)
    assert find_reports(tmp_path) == [ComponentReport.model_validate_json(written.read_text())]
    assert (tmp_path / report.report_id / "README.md").read_text() == report_markdown(report)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "report.json").write_text("{")
    assert len(find_reports(tmp_path)) == 1  # a broken file is skipped


def test_a_batch_made_before_w4_or_unfit_vectors_are_refused_never_scored() -> None:
    from evals.records import load_batch
    from recap.core.errors import ConfigError

    published = REPO_ROOT / "evals" / "reports" / "batches"
    v1 = load_batch(published / "20261025-114334-c205fe" / "batch.json")
    with pytest.raises(ConfigError, match="before M6 W4"):
        component_report(batch=v1, vectors=None, vectors_path=None, settings=SETTINGS)
    path = REPO_ROOT / "evals" / "datasets" / "query-vectors-v1.json"
    vectors = QueryVectors.load(path, SETTINGS.retrieval.embedder)
    vectors.weights = "sha256:another-model"
    with pytest.raises(ConfigError, match="do not fit"):
        component_report(batch=None, vectors=vectors, vectors_path=path, settings=SETTINGS)
