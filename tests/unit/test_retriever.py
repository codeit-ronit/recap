"""KnowledgeRetriever: relevance, determinism, reranking and optional hybrid retrieval."""

from __future__ import annotations

import re
import sys

import pytest

from recap.core.config import REPO_ROOT
from recap.core.errors import ConfigError
from recap.core.interfaces import Reranker, RetrievalIndex
from recap.core.models import DocChunk, RetrievedChunk
from recap.knowledge.bm25 import BM25Index
from recap.knowledge.embeddings import INSTALL_HINT, cosine, reciprocal_rank_fusion
from recap.knowledge.hints import ActionHint, parse_action_hints
from recap.knowledge.ingest import load_documents
from recap.knowledge.retriever import KnowledgeRetriever, LexicalReranker

KNOWLEDGE_DIR = REPO_ROOT / "fixtures" / "knowledge"


@pytest.fixture(scope="module")
def retriever() -> KnowledgeRetriever:
    return KnowledgeRetriever.from_directory(KNOWLEDGE_DIR, root=REPO_ROOT)


def _chunk(chunk_id: str, heading: str, text: str, title: str = "Doc") -> DocChunk:
    return DocChunk(
        chunk_id=chunk_id,
        doc_path="x.md",
        title=title,
        heading=heading,
        text=text,
        checksum="0" * 12,
    )


def _result(chunk_id: str, heading: str, title: str, score: float, rank: int) -> RetrievedChunk:
    return RetrievedChunk(
        chunk=_chunk(chunk_id, heading, "body", title), score=score, rank=rank, retriever="bm25"
    )


class KeywordEmbedder:
    """Fake embedder: one dimension per concept, so 'heap' and 'memory' land close together."""

    name = "fake-keyword-embedder"
    concepts = (("memory", "heap", "oom"), ("release", "deploy", "rollback"), ("upstream",))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [float(any(word in text.lower() for word in words)) for words in self.concepts]
            for text in texts
        ]


# --- default BM25 retriever over the real runbooks ----------------------------------------


def test_default_retriever_is_bm25_with_the_lexical_reranker(retriever: KnowledgeRetriever) -> None:
    assert isinstance(retriever, RetrievalIndex)
    assert isinstance(LexicalReranker(), Reranker)
    assert retriever.name == "bm25"

    results = retriever.search("rollback", k=3)

    assert [r.rank for r in results] == [1, 2, 3]
    assert [r.score for r in results] == sorted((r.score for r in results), reverse=True)
    assert {(r.retriever, r.reranker) for r in results} == {("bm25", "lexical-heading-boost")}


@pytest.mark.parametrize(
    ("query", "expected_doc"),
    [
        ("OOMKilled memory limit restart", "runbook-memory-pressure.md"),
        ("error spike after release rollback", "runbook-bad-deployment.md"),
        ("upstream call failed dependency", "guide-cascading-failures.md"),
    ],
)
def test_the_relevant_doc_ranks_first(
    retriever: KnowledgeRetriever, query: str, expected_doc: str
) -> None:
    top = retriever.search(query, k=4)[0]
    assert top.chunk.doc_path == f"fixtures/knowledge/{expected_doc}"


@pytest.mark.parametrize(
    ("mechanism", "action", "chunk_id"),
    [
        ("bad_deployment", "execute_rollback", "runbook-bad-deployment#remediation-1"),
        ("resource_pressure", "restart_service", "runbook-memory-pressure#remediation-1"),
    ],
)
@pytest.mark.parametrize(
    "template",
    # The exact mechanism name, and plain words as the controller's proposal-stage query uses.
    ["{name} remediation", "{words} remediation recommended action runbook"],
)
def test_mechanism_queries_surface_the_remediation_hint(
    retriever: KnowledgeRetriever, template: str, mechanism: str, action: str, chunk_id: str
) -> None:
    query = template.format(name=mechanism, words=mechanism.replace("_", " "))

    top = retriever.search(query, k=1)[0]

    assert top.chunk.chunk_id == chunk_id
    assert parse_action_hints(top.chunk.text) == [ActionHint(mechanism=mechanism, action=action)]


def test_search_is_deterministic() -> None:
    first = KnowledgeRetriever.from_directory(KNOWLEDGE_DIR, root=REPO_ROOT)
    second = KnowledgeRetriever.from_directory(KNOWLEDGE_DIR, root=REPO_ROOT)

    for query in ["memory restart", "rollback release", "upstream dependency", "approval"]:
        assert first.search(query, k=6) == second.search(query, k=6)


def test_k_and_empty_queries(retriever: KnowledgeRetriever) -> None:
    assert len(retriever.search("rollback", k=2)) == 2
    assert retriever.search("rollback", k=0) == []
    assert retriever.search("   ", k=4) == []
    assert retriever.search("the and of", k=4) == []  # only stopwords


def test_without_a_reranker_scores_are_plain_bm25_over_heading_and_text() -> None:
    chunks = load_documents(KNOWLEDGE_DIR, root=REPO_ROOT)
    plain = KnowledgeRetriever(chunks)

    results = plain.search("rollback revision", k=5)
    expected = BM25Index([(c.chunk_id, f"{c.heading}\n{c.text}") for c in chunks]).search(
        "rollback revision", 5
    )

    assert [(r.chunk.chunk_id, r.score) for r in results] == expected
    assert all(r.reranker is None for r in results)


def test_chunks_property_returns_a_copy(retriever: KnowledgeRetriever) -> None:
    retriever.chunks.clear()
    assert len(retriever.chunks) > 0


# --- lexical reranker ----------------------------------------------------------------------


def test_reranker_boosts_heading_and_title_matches() -> None:
    results = [
        _result("a#notes-1", "Notes", "Misc", score=1.0, rank=1),
        _result("b#remediation-1", "Remediation", "Runbook: bad deployment", score=0.9, rank=2),
        _result("c#other-1", "Other", "Other", score=0.5, rank=3),
    ]

    reranked = LexicalReranker().rerank("bad deployment remediation", results)

    # All three query words appear in b's heading or title: 0.9 * (1 + 0.25 * 3/3) = 1.125.
    assert [r.chunk.chunk_id for r in reranked] == ["b#remediation-1", "a#notes-1", "c#other-1"]
    assert reranked[0].score == pytest.approx(1.125)
    assert [r.score for r in reranked[1:]] == [1.0, 0.5]  # no heading match, no boost


def test_reranker_keeps_every_result_renumbers_ranks_and_is_stable() -> None:
    results = [_result(f"d{i}#s-1", "Section", "Doc", score=1.0, rank=10 * i) for i in range(1, 6)]

    reranked = LexicalReranker().rerank("unrelated words", results)

    assert [r.chunk.chunk_id for r in reranked] == [r.chunk.chunk_id for r in results]
    assert [r.rank for r in reranked] == [1, 2, 3, 4, 5]
    assert {r.reranker for r in reranked} == {"lexical-heading-boost"}


# --- optional hybrid retrieval (embeddings behind a feature flag) --------------------------


def test_hybrid_retrieval_finds_a_match_that_bm25_misses() -> None:
    chunks = [
        _chunk("a#s-1", "Release notes", "Roll back the release."),
        _chunk("b#s-1", "Pods", "Restart crashed pods."),
        _chunk("c#s-1", "Capacity", "Containers ran out of memory."),
    ]
    query = "heap exhaustion"  # shares no word with any chunk

    assert KnowledgeRetriever(chunks).search(query) == []

    hybrid = KnowledgeRetriever(chunks, embedder=KeywordEmbedder())
    results = hybrid.search(query)

    assert hybrid.name == "hybrid-bm25-embeddings"
    assert [(r.chunk.chunk_id, r.retriever) for r in results] == [
        ("c#s-1", "hybrid-bm25-embeddings")
    ]
    assert results[0].score == pytest.approx(1 / 61)  # RRF: rank 1 in the dense list only


def test_reciprocal_rank_fusion_rewards_agreement_and_breaks_ties_by_id() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["b", "c", "a"]])

    assert [item for item, _ in fused] == ["b", "a", "c"]
    assert dict(fused)["b"] == pytest.approx(1 / 61 + 1 / 62)
    assert [item for item, _ in reciprocal_rank_fusion([["x", "y"], ["y", "x"]])] == ["x", "y"]
    # Repeats inside one ranking count once, at their best rank.
    assert reciprocal_rank_fusion([["a", "a", "b"]]) == [
        ("a", pytest.approx(1 / 61)),
        ("b", pytest.approx(1 / 62)),
    ]


def test_cosine_similarity() -> None:
    assert cosine([1.0, 2.0], [2.0, 4.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # a zero vector is similar to nothing
    with pytest.raises(ValueError):
        cosine([1.0], [1.0, 2.0])


def test_enabling_embeddings_without_the_package_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A None entry in sys.modules makes `import sentence_transformers` fail, as if the
    # optional package were not installed (this works whether or not it really is).
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    with pytest.raises(ConfigError, match=re.escape(INSTALL_HINT)):
        KnowledgeRetriever.from_directory(KNOWLEDGE_DIR, root=REPO_ROOT, enable_embeddings=True)
