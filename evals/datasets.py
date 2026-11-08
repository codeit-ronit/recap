"""The W4 evaluation datasets (M6 W4), derived from the fixtures and written to evals/datasets/
so they can be read and reviewed:

    retrieval-v1.yaml   queries and what counts as relevant to each:
                          alert       every scenario's alert, exactly as RECAP searches it
                                      (its service and symptoms: the controller's first query)
                          proposal    the controller's remediation lookup, for each mechanism
                                      a runbook covers
                          paraphrase  hand-written (retrieval-paraphrases-v1.yaml)
    labels-v1.yaml      per scenario: partition, alerted and root service, mechanism, the exact
                        fix, and whether the cause sits behind the alerted service; and every
                        (scored scenario, train scenario) pair labelled twin, same_diagnosis or
                        look_alike: would reusing the train episode's fix be right today?

Relevance comes from the runbooks themselves: a chunk is relevant to a query about mechanism
M when its document declares M (a "Mechanism: M" line), and is a remediation chunk when it
holds a "Recommended action:" for M (what the controller needs before it proposes a fix; the
cascading-failures guide deliberately gives none for dependency_failure). The answers come
from each scenario's `expected` block, which no agent ever sees. These files are read by the
scorer (evals/components.py) only: never by RECAP's retriever, memory or router
(tests/unit/test_w4_leakage.py checks that).

    python -m evals.datasets          # rewrite both files (a test checks they are current)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

import yaml

from recap.core.config import REPO_ROOT
from recap.core.models import Contract, FailureMechanism
from recap.knowledge.hints import declared_mechanisms, parse_action_hints
from recap.knowledge.ingest import load_documents

DATASETS_DIR = REPO_ROOT / "evals" / "datasets"
RETRIEVAL_FILE = DATASETS_DIR / "retrieval-v1.yaml"
LABELS_FILE = DATASETS_DIR / "labels-v1.yaml"
PARAPHRASES_FILE = DATASETS_DIR / "retrieval-paraphrases-v1.yaml"
SCENARIOS_DIR = REPO_ROOT / "fixtures" / "scenarios"
KNOWLEDGE_DIR = REPO_ROOT / "fixtures" / "knowledge"
# The red-team overlay: a poisoned runbook that urges an uninvestigated restart. Retrieval is
# also measured with it loaded, to count how often it reaches a search's results.
POISONED_DIR = REPO_ROOT / "fixtures" / "knowledge-overlays" / "poisoned"

QueryKind = Literal["alert", "proposal", "paraphrase"]
PairLabel = Literal["twin", "same_diagnosis", "look_alike"]


class RetrievalQuery(Contract):
    id: str
    kind: QueryKind
    text: str
    mechanism: str  # whose runbook answers it
    source: str  # the scenario, the mechanism, or "hand-written"
    partition: str = ""  # an alert query's scenario partition (train, test, demo)


class RetrievalDataset(Contract):
    schema_version: Literal["recap-retrieval-dataset-v1"] = "recap-retrieval-dataset-v1"
    corpus: str  # the knowledge folder searched
    chunks: int
    # Per mechanism: the chunks of every document that declares it, and its remediation chunks.
    relevant: dict[str, list[str]]
    remediation: dict[str, list[str]]
    poisoned: list[str]  # the red-team overlay's chunks
    not_scored: list[str]  # mechanisms no document declares: no query can be judged for them
    queries: list[RetrievalQuery]


class ScenarioLabel(Contract):
    partition: str
    family: str
    alert_service: str
    root_service: str
    mechanism: str
    fix: str  # e.g. "restart_service on cart", "execute_rollback on cart (to_version=v2.1.0)"
    cross_service: bool  # the root cause sits behind the alerted service


class MemoryPair(Contract):
    scenario: str  # a scored scenario (test partition)
    remembered: str  # a train scenario, whose episode the evaluation memory holds
    # twin: the same exact fix is right today; same_diagnosis: same service and mechanism but
    # another fix (a rollback to another version); look_alike: another service or mechanism.
    label: PairLabel


class LabelSet(Contract):
    schema_version: Literal["recap-labels-v1"] = "recap-labels-v1"
    scenarios: dict[str, ScenarioLabel]
    memory_pairs: list[MemoryPair]


# --- building -------------------------------------------------------------------------------


def scenario_files(root: Path = SCENARIOS_DIR) -> dict[str, dict[str, Any]]:
    """Every scenario file's contents, by scenario id."""
    found = {}
    for path in sorted(root.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        found[str(data["id"])] = data
    return found


def alert_query(scenario: dict[str, Any]) -> str:
    """The controller's first runbook query for this scenario: the alert's service and
    symptoms (IncidentController._retrieve)."""
    alert = scenario["alert"]
    return " ".join([alert["service"], *alert["symptoms"]])


def proposal_query(mechanism: str) -> str:
    """The controller's remediation lookup (IncidentController._maybe_retrieve_runbook)."""
    return f"{mechanism.replace('_', ' ')} remediation recommended action runbook"


def build_retrieval(
    scenarios: dict[str, dict[str, Any]], knowledge: Path = KNOWLEDGE_DIR
) -> RetrievalDataset:
    chunks = load_documents(knowledge, root=REPO_ROOT)
    remediation: dict[str, list[str]] = {}
    covering: dict[str, set[str]] = {}
    for chunk in chunks:
        for mechanism in declared_mechanisms(chunk.text):
            covering.setdefault(mechanism, set()).add(chunk.doc_path)
        for hint in parse_action_hints(chunk.text):
            remediation.setdefault(hint.mechanism, []).append(chunk.chunk_id)
    relevant = {
        mechanism: [c.chunk_id for c in chunks if c.doc_path in docs]
        for mechanism, docs in sorted(covering.items())
    }
    queries = [
        RetrievalQuery(
            id=f"alert-{sid}",
            kind="alert",
            text=alert_query(data),
            mechanism=str(data["expected"]["mechanism"]),
            source=sid,
            partition=str(data["partition"]),
        )
        for sid, data in scenarios.items()
        if str(data.get("expected", {}).get("mechanism")) in relevant
    ]
    queries += [  # the controller looks up a remediation only for a mechanism that has one
        RetrievalQuery(
            id=f"proposal-{mechanism}",
            kind="proposal",
            text=proposal_query(mechanism),
            mechanism=mechanism,
            source=mechanism,
        )
        for mechanism in relevant
        if mechanism in remediation
    ]
    hand = yaml.safe_load(PARAPHRASES_FILE.read_text(encoding="utf-8"))["queries"]
    queries += [
        RetrievalQuery(
            id=str(q["id"]),
            kind="paraphrase",
            text=str(q["text"]),
            mechanism=str(q["mechanism"]),
            source="hand-written",
        )
        for q in hand
    ]
    poisoned = load_documents(POISONED_DIR, root=REPO_ROOT) if POISONED_DIR.is_dir() else []
    return RetrievalDataset(
        corpus=knowledge.relative_to(REPO_ROOT).as_posix(),
        chunks=len(chunks),
        relevant=relevant,
        remediation={m: remediation[m] for m in sorted(remediation)},
        poisoned=[c.chunk_id for c in poisoned],
        not_scored=[m.value for m in FailureMechanism if m.value not in relevant],
        queries=queries,
    )


def exact_fix(scenario: dict[str, Any]) -> str:
    """The scenario's fix as a proposal would state it: the action, its target and, for a
    rollback, the version before the bad release (the root service's previous deployment)."""
    expected = scenario["expected"]
    root = str(expected["root_cause_service"])
    action = str(expected["fix"])
    if action != "execute_rollback":
        return f"{action} on {root}"
    bad = next(
        str(f["version"])
        for f in scenario["faults"]
        if f["type"] == "bad_release" and f["service"] == root
    )
    history = sorted(
        (d for d in scenario["deployments"] if d["service"] == root and d["version"] != bad),
        key=lambda d: d["minutes_ago"],
    )
    return f"{action} on {root} (to_version={history[0]['version']})"


def build_labels(scenarios: dict[str, dict[str, Any]]) -> LabelSet:
    labels = {
        sid: ScenarioLabel(
            partition=str(data["partition"]),
            family=str(data["family"]),
            alert_service=str(data["alert"]["service"]),
            root_service=str(data["expected"]["root_cause_service"]),
            mechanism=str(data["expected"]["mechanism"]),
            fix=exact_fix(data),
            cross_service=data["alert"]["service"] != data["expected"]["root_cause_service"],
        )
        for sid, data in scenarios.items()
    }
    train = [sid for sid, label in labels.items() if label.partition == "train"]
    pairs = []
    for sid, label in labels.items():
        if label.partition != "test":
            continue
        for remembered in train:
            past = labels[remembered]
            same = (past.root_service, past.mechanism) == (label.root_service, label.mechanism)
            verdict: PairLabel = (
                "twin" if past.fix == label.fix else "same_diagnosis" if same else "look_alike"
            )
            pairs.append(MemoryPair(scenario=sid, remembered=remembered, label=verdict))
    return LabelSet(scenarios=labels, memory_pairs=pairs)


def render(model: Contract, header: str) -> str:
    body = yaml.safe_dump(
        model.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=100
    )
    return header + body


RETRIEVAL_HEADER = (
    "# GENERATED by `python -m evals.datasets` from fixtures/scenarios, fixtures/knowledge and\n"
    "# evals/datasets/retrieval-paraphrases-v1.yaml: do not edit (see evals/datasets/README.md).\n"
)
LABELS_HEADER = (
    "# GENERATED by `python -m evals.datasets` from fixtures/scenarios (each scenario's\n"
    "# evaluation-only `expected` block): do not edit (see evals/datasets/README.md).\n"
)


def expected_files() -> dict[Path, str]:
    """Each dataset file's path and the contents it should have now."""
    scenarios = scenario_files()
    return {
        RETRIEVAL_FILE: render(build_retrieval(scenarios), RETRIEVAL_HEADER),
        LABELS_FILE: render(build_labels(scenarios), LABELS_HEADER),
    }


def load_retrieval(path: Path = RETRIEVAL_FILE) -> RetrievalDataset:
    return RetrievalDataset.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_labels(path: Path = LABELS_FILE) -> LabelSet:
    return LabelSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    for path, text in expected_files().items():
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
