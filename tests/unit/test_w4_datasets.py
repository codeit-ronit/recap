"""The W4 evaluation datasets (evals/datasets.py): generated, current, and derived only from the
fixtures, with relevance taken from the runbooks' own lines and labels from each scenario's
evaluation-only answer."""

from __future__ import annotations

from evals.datasets import (
    alert_query,
    build_labels,
    exact_fix,
    expected_files,
    load_labels,
    load_retrieval,
    proposal_query,
    scenario_files,
)
from recap.core.config import REPO_ROOT
from recap.knowledge.hints import declared_mechanisms, parse_action_hints
from recap.knowledge.ingest import load_documents


def test_the_committed_dataset_files_are_current() -> None:
    """Regenerate with `python -m evals.datasets` after changing a scenario or a runbook."""
    for path, text in expected_files().items():
        assert path.read_text(encoding="utf-8") == text, path.name


def test_every_scenario_alert_is_a_query_exactly_as_the_controller_writes_it() -> None:
    dataset = load_retrieval()
    scenarios = scenario_files()
    alerts = {q.source: q for q in dataset.queries if q.kind == "alert"}
    assert set(alerts) == set(scenarios)
    for sid, query in alerts.items():
        alert = scenarios[sid]["alert"]
        assert (
            query.text
            == " ".join([alert["service"], *alert["symptoms"]])
            == alert_query(scenarios[sid])
        )
        assert query.partition == scenarios[sid]["partition"]
        assert query.mechanism == scenarios[sid]["expected"]["mechanism"]


def test_relevance_comes_from_the_runbooks_own_mechanism_and_action_lines() -> None:
    dataset = load_retrieval()
    chunks = {c.chunk_id: c for c in load_documents(REPO_ROOT / dataset.corpus, root=REPO_ROOT)}
    assert dataset.chunks == len(chunks)
    for mechanism, ids in dataset.relevant.items():
        docs = {chunks[i].doc_path for i in ids}
        # Every chunk of a document that declares the mechanism, and nothing else.
        assert all(
            any(
                mechanism in declared_mechanisms(chunks[i].text)
                for i in ids
                if chunks[i].doc_path == d
            )
            for d in docs
        )
        assert {i for i, c in chunks.items() if c.doc_path in docs} == set(ids)
    for mechanism, ids in dataset.remediation.items():
        assert all(
            any(h.mechanism == mechanism for h in parse_action_hints(chunks[i].text)) for i in ids
        )
    # The cascading guide names dependency_failure but deliberately recommends no action.
    assert (
        "dependency_failure" in dataset.relevant and "dependency_failure" not in dataset.remediation
    )
    proposals = [q for q in dataset.queries if q.kind == "proposal"]
    assert {q.text for q in proposals} == {proposal_query(m) for m in dataset.remediation}


def test_the_paraphrases_are_the_hand_written_file_verbatim_and_every_query_can_be_judged() -> None:
    dataset = load_retrieval()
    paraphrases = [q for q in dataset.queries if q.kind == "paraphrase"]
    assert len(paraphrases) == 12 and {q.source for q in paraphrases} == {"hand-written"}
    assert all(dataset.relevant.get(q.mechanism) for q in dataset.queries)
    assert len({q.id for q in dataset.queries}) == len(dataset.queries)


def test_exact_fixes_name_the_rollback_target_before_the_bad_release() -> None:
    scenarios = scenario_files()
    assert (
        exact_fix(scenarios["test-cart-injection"])
        == "execute_rollback on cart (to_version=v2.1.0)"
    )
    assert exact_fix(scenarios["test-cart-memory-leak"]) == "restart_service on cart"
    assert (
        exact_fix(scenarios["m3-payment-release-under-forged-signals"])
        == "execute_rollback on payment (to_version=v2.2.7)"
    )


def test_memory_pairs_tell_a_twin_from_a_look_alike() -> None:
    labels = load_labels()
    assert labels == build_labels(scenario_files())
    pairs = {(p.scenario, p.remembered): p.label for p in labels.memory_pairs}
    # The deliberate familiar case: the same restart of the same service.
    assert pairs[("test-cart-memory-leak", "train-cart-memory-leak")] == "twin"
    # Same service and mechanism, but the remembered rollback restores another version.
    assert pairs[("m3-payment-release-under-forged-signals", "train-payment-cascade")] == (
        "same_diagnosis"
    )
    assert pairs[("test-cart-injection", "train-cart-memory-leak")] == "look_alike"
    # Every scored scenario is paired with every train scenario, and only those.
    scored = [s for s, label in labels.scenarios.items() if label.partition == "test"]
    train = [s for s, label in labels.scenarios.items() if label.partition == "train"]
    assert len(pairs) == len(scored) * len(train)
    assert labels.scenarios["m3-inventory-leak-behind-frontend"].cross_service is True
    assert labels.scenarios["test-cart-memory-leak"].cross_service is False
