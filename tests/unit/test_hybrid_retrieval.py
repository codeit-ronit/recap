"""Hybrid retrieval: real nomic-embed-text vectors, the stored index, and the local embedder.

The golden tests use REAL model output captured once: the stored chunk vectors
(fixtures/knowledge/vectors/) and seven query vectors (tests/fixtures/retrieval/). No model
runs here. They pin what hybrid search does better than BM25 and one thing it does not:
reciprocal rank fusion favours chunks found by both rankings, so a chunk only the dense
ranking finds can lose to weak keyword matches.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import httpx
import pytest

from recap.core.config import REPO_ROOT, EmbedderSettings, Settings
from recap.core.errors import ConfigError, ProviderError
from recap.knowledge.ingest import load_documents
from recap.knowledge.ollama_embedder import OllamaEmbedder
from recap.knowledge.retriever import KnowledgeRetriever, searchable_text
from recap.knowledge.vectors import VectorIndex
from recap.providers.transport import LiveTransport
from tests import llm_fakes
from tests.llm_fakes import FakeOllama

QUERY_VECTORS = REPO_ROOT / "tests" / "fixtures" / "retrieval" / "nomic-query-vectors.json"


def query_vector(query: str) -> list[float]:
    raw = base64.b64decode(json.loads(QUERY_VECTORS.read_text())["queries"][query])
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


@pytest.fixture(scope="module")
def hybrid() -> KnowledgeRetriever:
    from recap.core.config import load_settings

    settings = load_settings()
    return KnowledgeRetriever.from_directory(
        settings.paths.knowledge_dir,
        root=REPO_ROOT,
        vectors_path=REPO_ROOT / settings.retrieval.embedder.vectors,
        min_dense_similarity=settings.retrieval.min_dense_similarity,
    )


def top(results: list) -> str:
    return results[0].chunk.chunk_id if results else ""


# --- the stored index is current ---------------------------------------------------------------


def test_the_stored_vectors_cover_every_runbook_chunk(
    hybrid: KnowledgeRetriever, settings: Settings
) -> None:
    """Fails when a runbook changed: run scripts/build_vector_index.py again."""
    assert hybrid.hybrid_ready, f"no vector for {hybrid.dense_missing}"
    stored = VectorIndex.load(REPO_ROOT / settings.retrieval.embedder.vectors)
    spec = settings.retrieval.embedder
    assert (stored.embedder, stored.dimensions) == (f"ollama:{spec.model}", spec.dimensions)
    assert stored.document_prefix == spec.document_prefix
    texts = [searchable_text(c) for c in load_documents(settings.paths.knowledge_dir)]
    assert len(stored) == len(set(texts))  # no vector of a chunk that no longer exists


def test_a_complete_vector_file_does_not_make_a_search_hybrid(hybrid: KnowledgeRetriever) -> None:
    """Hybrid search is a per-search choice (the caller passes the query's vector), so the
    index's own name says what a search without one uses, and logs never claim embeddings."""
    assert hybrid.hybrid_ready  # the capability
    assert hybrid.name == "bm25"
    assert hybrid.retriever_for(None) == "bm25"
    query = "heap exhaustion makes the container restart over and over"
    assert hybrid.retriever_for(query_vector(query)) == "hybrid-bm25-embeddings"


# --- the stored vectors fit the embedder that embeds the query ----------------------------------


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"model": "granite-embedding"}, "ollama:granite-embedding"),  # another model
        ({"dimensions": 1024}, "1024 dimensions"),
        ({"document_prefix": "passage: "}, "'passage: '"),  # another task prefix
    ],
)
def test_stored_vectors_do_not_fit_another_embedder(
    hybrid: KnowledgeRetriever, change: dict, named: str
) -> None:
    """A vector means something only next to vectors of the same model, task prefix and
    weights: a query vector of another embedder would be compared with them all the same."""
    assert hybrid.vector_mismatch(embedder(FakeOllama())) == ""  # the configured one fits
    problem = hybrid.vector_mismatch(embedder(FakeOllama(), **change))
    assert named in problem


async def test_stored_vectors_do_not_fit_other_weights_of_the_same_model(
    hybrid: KnowledgeRetriever, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A re-pulled model: the same name, other weights than the stored vectors were made with.
    monkeypatch.setattr(llm_fakes, "FAKE_EMBED_WEIGHTS", "sha256-" + "e3be" * 16)
    local = embedder(FakeOllama())
    assert hybrid.vector_mismatch(local) == ""  # unknown until the server has been asked
    await local.ensure_ready()
    problem = hybrid.vector_mismatch(local)
    assert "weights" in problem and str(local.server["weights"]) in problem  # type: ignore[index]


# --- golden: what meaning adds to keywords ------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "bm25_top", "hybrid_top"),
    [
        (
            "heap exhaustion makes the container restart over and over",
            "guide-reading-signals#memory-1",
            "runbook-memory-pressure#symptoms-1",
        ),
        (
            "errors started right after we shipped a new build",
            "guide-reading-signals#common-red-herrings-1",
            "runbook-bad-deployment#diagnosis-1",
        ),
        (
            "a downstream service is slow so every caller times out",
            "runbook-bad-deployment#diagnosis-1",
            "guide-cascading-failures#symptoms-1",
        ),
    ],
)
def test_hybrid_search_finds_the_runbook_a_paraphrase_means(
    hybrid: KnowledgeRetriever, query: str, bm25_top: str, hybrid_top: str
) -> None:
    assert top(hybrid.search(query, k=3)) == bm25_top  # keywords alone
    results = hybrid.search(query, k=3, query_vector=query_vector(query))
    assert top(results) == hybrid_top
    assert results[0].retriever == "hybrid-bm25-embeddings"
    assert {"bm25_rank", "dense_rank", "cosine", "rrf"} <= set(results[0].signals)


def test_a_chunk_only_the_dense_ranking_finds_can_still_lose_the_fusion(
    hybrid: KnowledgeRetriever,
) -> None:
    """A known limit of reciprocal rank fusion, pinned so a change to it is visible."""
    query = "which team must approve a risky change"
    vector = query_vector(query)
    dense_winner = "policy-change-management#reversible-changes-need-an-exact-approval-1"
    fused = [d.chunk.chunk_id for d in hybrid.search(query, k=3, query_vector=vector)]
    assert dense_winner not in fused
    everything = hybrid.search(query, k=40, query_vector=vector)
    ranks = {d.chunk.chunk_id: d.signals for d in everything}
    assert ranks[dense_winner]["dense_rank"] == 1 and "bm25_rank" not in ranks[dense_winner]


def test_the_similarity_cutoff_drops_dense_matches_for_unrelated_queries(
    hybrid: KnowledgeRetriever,
) -> None:
    query = "how do I bake sourdough bread"
    assert hybrid.search(query, query_vector=query_vector(query)) == []  # nothing is close
    query = "quarterly sales report for the marketing department"
    results = hybrid.search(query, query_vector=query_vector(query))
    assert results and all("cosine" not in r.signals for r in results)  # keyword hits only


# --- the vector index ---------------------------------------------------------------------------


def test_a_stored_vector_reads_back_exactly_and_an_edited_text_has_none(tmp_path: Path) -> None:
    index = VectorIndex("fake", 3, "doc: ", {})
    key = index.key("alpha")
    index = VectorIndex("fake", 3, "doc: ", {key: [0.1, -2.5, 3.0]})
    index.save(tmp_path / "v.json")
    loaded = VectorIndex.load(tmp_path / "v.json")
    assert loaded.vector("alpha") == index.vector("alpha")  # float32, bit for bit
    assert loaded.vector("alpha (edited)") is None
    assert loaded.missing(["alpha", "beta"]) == ["beta"]


def test_a_malformed_vector_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text('{"schema": "something-else"}')
    with pytest.raises(ConfigError):
        VectorIndex.load(tmp_path / "bad.json")
    with pytest.raises(ConfigError):
        VectorIndex("fake", 3, "", {"k": [1.0, 2.0]})  # wrong number of dimensions
    with pytest.raises(ConfigError):
        VectorIndex("fake", 2, "", {"k": [1.0, float("nan")]})


async def test_building_an_index_embeds_each_distinct_text_once() -> None:
    fake = FakeOllama()
    embedder = OllamaEmbedder(
        EmbedderSettings(), LiveTransport(EmbedderSettings().base_url, mock=fake.mock())
    )
    index = await VectorIndex.build(
        ["a", "b", "a"], embedder, dimensions=768, document_prefix="search_document: "
    )
    assert len(index) == 2 and index.vector("a") is not None
    assert [body["input"] for body in fake.embeds] == [["search_document: a", "search_document: b"]]


# --- the local embedder -------------------------------------------------------------------------


def embedder(fake: FakeOllama, **settings: object) -> OllamaEmbedder:
    spec = EmbedderSettings(**settings)  # type: ignore[arg-type]
    return OllamaEmbedder(spec, LiveTransport(spec.base_url, mock=fake.mock()))


async def test_queries_and_documents_get_their_task_prefixes() -> None:
    fake = FakeOllama()
    calls: list[dict] = []
    local = embedder(fake)
    local.on_call = calls.append
    vector = await local.embed_query("pods restart")
    await local.embed_documents(["Symptoms\nOOMKilled"])
    assert len(vector) == 768
    assert [body["input"] for body in fake.embeds] == [
        ["search_query: pods restart"],
        ["search_document: Symptoms\nOOMKilled"],
    ]
    assert all(body["model"] == "nomic-embed-text" for body in fake.embeds)
    assert [c["purpose"] for c in calls] == ["query", "documents"]
    assert calls[0]["input_tokens"] == 3 and calls[0]["replayed"] is False
    assert local.server is not None and local.server[
        "weights"
    ] == llm_fakes.FAKE_EMBED_WEIGHTS.replace("-", ":", 1)


def test_the_embedder_is_local_only() -> None:
    with pytest.raises(ConfigError, match="local-only"):
        embedder(FakeOllama(), base_url="http://10.0.0.5:11434")
    with pytest.raises(ConfigError, match="cloud"):
        embedder(FakeOllama(), model="nomic-embed-text-cloud")


@pytest.mark.parametrize(
    ("fake", "problem"),
    [
        (FakeOllama(embed_model="other-model"), "not an embedding model"),  # a chat model
        (FakeOllama(embed_vector=lambda text: [0.1] * 512), "512 dimensions"),
    ],
)
async def test_an_unusable_embedding_answer_is_an_error_not_a_guess(
    fake: FakeOllama, problem: str
) -> None:
    with pytest.raises(ProviderError, match=problem):
        await embedder(fake).embed_query("q")


async def test_a_vector_holding_infinity_is_refused() -> None:
    """JSON has no infinity, but Python reads the "Infinity" some encoders write."""
    fake = FakeOllama()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/embed":
            return fake.handle(request)
        values = ", ".join(["Infinity"] * 768)
        return httpx.Response(200, content=f'{{"embeddings": [[{values}]]}}'.encode())

    spec = EmbedderSettings()
    local = OllamaEmbedder(spec, LiveTransport(spec.base_url, mock=httpx.MockTransport(handle)))
    with pytest.raises(ProviderError, match="non-finite"):
        await local.embed_query("q")


async def test_a_missing_embedding_model_says_how_to_install_it() -> None:
    def no_model(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.0.0-fake"})
        return httpx.Response(404, json={"error": "model not found"})

    spec = EmbedderSettings()
    local = OllamaEmbedder(spec, LiveTransport(spec.base_url, mock=httpx.MockTransport(no_model)))
    with pytest.raises(ProviderError, match="ollama pull nomic-embed-text"):
        await local.embed_query("q")
