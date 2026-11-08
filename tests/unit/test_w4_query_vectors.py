"""Stored query vectors (M6 W4): hybrid retrieval in an evaluation answers from a captured file,
never contacts a model server, refuses a query it has no vector for, and fits the runbook
vectors only when it comes from the same model, prefix and weights."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evals.datasets import load_retrieval
from evals.query_vectors import QueryVectors, StoredQueryEmbedder
from recap.core.config import REPO_ROOT, load_settings
from recap.core.errors import ConfigError
from recap.knowledge.vectors import VectorIndex

SPEC = load_settings(target="").retrieval.embedder
COMMITTED = REPO_ROOT / "evals" / "datasets" / "query-vectors-v1.json"
RUNBOOK_VECTORS = REPO_ROOT / SPEC.vectors


class FakeLive:
    """A local embedder stand-in: a fixed vector per text, and a server that reports weights."""

    name = f"ollama:{SPEC.model}"

    def __init__(self, weights: str | None = "sha256:abc") -> None:
        self.calls: list[str] = []
        self.server: dict[str, str | None] | None = None
        self._weights = weights

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("documents are never embedded here")

    async def embed_query(self, text: str) -> list[float]:
        self.calls.append(text)
        self.server = {"weights": self._weights, "ollama_version": "test"}
        return [float(len(text) % 7 + i) for i in range(SPEC.dimensions)]


def test_a_stored_vector_is_answered_with_no_server_and_counted_as_stored() -> None:
    vectors = QueryVectors.load(COMMITTED, SPEC)
    query = load_retrieval().queries[0].text
    embedder = vectors.embedder()
    calls: list[dict[str, object]] = []
    embedder.on_call = calls.append
    vector = asyncio.run(embedder.embed_query(query))
    assert vector == vectors.index.vector(query) and len(vector) == SPEC.dimensions
    assert calls and calls[0]["replayed"] is True and calls[0]["purpose"] == "query"


def test_a_query_without_a_stored_vector_is_refused_not_approximated() -> None:
    vectors = QueryVectors.load(COMMITTED, SPEC)
    embedder = vectors.embedder()
    with pytest.raises(ConfigError, match="capture them again"):
        asyncio.run(embedder.embed_query("a query nobody captured"))
    assert vectors.missing == ["a query nobody captured"]  # the runner stops on it
    with pytest.raises(ConfigError, match="queries only"):
        asyncio.run(embedder.embed_documents(["a runbook"]))


def test_the_stored_embedder_fits_the_runbook_vectors_as_the_live_one_would() -> None:
    runbooks = VectorIndex.load(RUNBOOK_VECTORS)
    embedder = QueryVectors.load(COMMITTED, SPEC).embedder()
    assert isinstance(embedder, StoredQueryEmbedder)
    assert runbooks.mismatch(embedder) == ""  # model, dimensions, document prefix, weights
    assert embedder.server is not None and embedder.server["weights"] == runbooks.meta["weights"]


def test_the_committed_file_covers_every_dataset_query_and_names_its_weights() -> None:
    vectors = QueryVectors.load(COMMITTED, SPEC)
    queries = load_retrieval().queries
    assert all(vectors.lookup(q.text) is not None for q in queries)
    assert vectors.weights == VectorIndex.load(RUNBOOK_VECTORS).meta["weights"]
    assert set(vectors.index.meta["queries"]) == {q.text for q in queries}  # readable, all of them


def test_capturing_embeds_only_what_is_missing_and_saves_it(tmp_path: Path) -> None:
    path = tmp_path / "queries.json"
    live = FakeLive()
    vectors = QueryVectors.empty(SPEC, live, weights="sha256:abc")
    embedder = vectors.embedder()
    asyncio.run(embedder.embed_query("first"))
    asyncio.run(embedder.embed_query("first"))  # kept: not embedded twice
    assert live.calls == ["first"]
    assert vectors.save(path) == 1
    again = QueryVectors.load(path, SPEC)
    assert again.lookup("first") is not None and again.weights == "sha256:abc"
    assert again.index.meta["queries"] == ["first"]
    # Re-capturing into the same file adds only the new query.
    more = FakeLive()
    again.capturing(more)
    asyncio.run(again.embedder().embed_query("first"))
    asyncio.run(again.embedder().embed_query("second"))
    assert more.calls == ["second"] and again.save(path) == 1


def test_a_server_with_other_weights_is_refused_while_capturing(tmp_path: Path) -> None:
    vectors = QueryVectors.empty(SPEC, FakeLive(weights="sha256:other"), weights="sha256:abc")
    with pytest.raises(ConfigError, match="other|weights"):
        asyncio.run(vectors.embedder().embed_query("q"))


def test_a_file_made_another_way_is_refused(tmp_path: Path) -> None:
    def index(**changes: object) -> VectorIndex:
        fields: dict[str, object] = {
            "embedder": f"ollama:{SPEC.model}",
            "dimensions": SPEC.dimensions,
            "document_prefix": SPEC.query_prefix,
            "vectors": {},
            "meta": {},
        }
        fields.update(changes)
        return VectorIndex(**fields)  # type: ignore[arg-type]

    with pytest.raises(ConfigError, match="made by"):
        QueryVectors(index(embedder="ollama:other"), SPEC)
    with pytest.raises(ConfigError, match="query prefix"):
        QueryVectors(index(document_prefix=SPEC.document_prefix), SPEC)
    with pytest.raises(ConfigError, match="dimensions"):
        QueryVectors(index(dimensions=3), SPEC)
    with pytest.raises(ConfigError, match="not queries"):
        QueryVectors(index(meta={"kind": "documents"}), SPEC)


def test_weights_that_nothing_reported_are_never_assumed(tmp_path: Path) -> None:
    """Fail closed: a server that reports no weights digest, or a file that names none, cannot
    prove its vectors fit the runbook vectors."""
    capturing = QueryVectors.empty(SPEC, FakeLive(weights=None), weights="sha256:abc")
    with pytest.raises(ConfigError, match="no weights digest"):
        asyncio.run(capturing.embedder().embed_query("q"))
    assert capturing.captured == {}
    path = tmp_path / "unweighed.json"
    VectorIndex(f"ollama:{SPEC.model}", SPEC.dimensions, SPEC.query_prefix, {}, {}).save(path)
    with pytest.raises(ConfigError, match="names no weights digest"):
        QueryVectors.load(path, SPEC)
