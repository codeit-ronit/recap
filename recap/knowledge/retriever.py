"""Knowledge retriever: the search entry point of RECAP's runbook RAG.

The controller (and, through the search_runbooks tool, an agent) asks
`KnowledgeRetriever.search(query)` for runbook chunks and records the cited results.
Retrieval runs in two stages:

    1. Candidates: BM25 keyword ranking (bm25.py). In hybrid mode the caller also passes
       the query's embedding, and a dense cosine ranking over the stored chunk vectors
       (vectors.py) is fused in with reciprocal rank fusion (embeddings.py). Dense
       candidates below `min_dense_similarity` are dropped first, so a query about
       something the runbooks never cover does not pull in a "closest" but unrelated one.
    2. Rerank: a `Reranker` re-orders the candidates. RECAP ships `LexicalReranker`, a
       simple heuristic, NOT a neural model; the interface lets a cross-encoder plug in.

Search is synchronous and pure: embedding the query (a network call to a local model) is
the caller's job, done once per search through a per-run embedder whose requests are
recorded and replayable. The same index and query vector always give the same results.

Retrieved text is UNTRUSTED data. This module ranks and cites it but never acts on it:
a poisoned runbook comes back verbatim, and the trusted safety policy downstream decides
what may actually run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from recap.core.interfaces import Reranker
from recap.core.models import DocChunk, RetrievedChunk
from recap.knowledge.bm25 import BM25Index, tokenize
from recap.knowledge.embeddings import cosine, reciprocal_rank_fusion
from recap.knowledge.ingest import load_documents
from recap.knowledge.vectors import VectorIndex

CANDIDATE_POOL = 20  # first-stage candidates handed to the reranker (at least k)
HEADING_BOOST = 0.25  # max score boost when every query word appears in the heading/title
BM25, HYBRID = "bm25", "hybrid-bm25-embeddings"


class LexicalReranker:
    """A simple lexical heuristic, NOT a neural reranker: boosts heading/title matches.

    A chunk's score is multiplied by (1 + 0.25 * the fraction of query words found in its
    heading or document title), because a section titled "Remediation" in "Runbook: bad
    deployment" is a better answer to "bad deployment remediation" than a passing mention.
    """

    name = "lexical-heading-boost"

    def rerank(self, query: str, results: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Same results, re-scored and re-sorted, with ranks renumbered from 1."""
        query_terms = set(tokenize(query))
        boosted = [
            result.model_copy(update={"score": result.score * self._boost(query_terms, result)})
            for result in results
        ]
        # sort() is stable: chunks with equal scores keep their first-stage order.
        boosted.sort(key=lambda result: -result.score)
        return [
            result.model_copy(update={"rank": rank, "reranker": self.name})
            for rank, result in enumerate(boosted, start=1)
        ]

    @staticmethod
    def _boost(query_terms: set[str], result: RetrievedChunk) -> float:
        """1.0 (no boost) up to 1 + HEADING_BOOST when all query words are in the heading."""
        if not query_terms:
            return 1.0
        heading_terms = set(tokenize(f"{result.chunk.heading} {result.chunk.title}"))
        return 1.0 + HEADING_BOOST * len(query_terms & heading_terms) / len(query_terms)


class KnowledgeRetriever:
    """Searches runbook chunks and returns ranked, cited `RetrievedChunk`s (RetrievalIndex).

    `vectors` holds the chunks' embeddings: a VectorIndex (looked up by each chunk's text)
    or, in tests, a mapping from chunk ID to vector. Hybrid search needs a vector for EVERY
    chunk; with any missing, `hybrid_ready` is False and `dense_missing` says which.
    """

    def __init__(
        self,
        chunks: list[DocChunk],
        *,
        reranker: Reranker | None = None,
        vectors: VectorIndex | Mapping[str, Sequence[float]] | None = None,
        min_dense_similarity: float = 0.0,
        candidate_pool: int = CANDIDATE_POOL,
    ) -> None:
        self._chunks = list(chunks)
        self._by_id = {chunk.chunk_id: chunk for chunk in self._chunks}
        self._reranker = reranker
        self.min_dense_similarity = min_dense_similarity
        self.candidate_pool = candidate_pool
        # The heading is indexed with the text: "Remediation" is a useful search word.
        documents = [(chunk.chunk_id, searchable_text(chunk)) for chunk in self._chunks]
        self._bm25 = BM25Index(documents)  # raises ValueError on duplicate chunk ids
        self.embedder_name: str | None = None
        self._dense: dict[str, list[float]] = {}
        if isinstance(vectors, VectorIndex):
            self.embedder_name = vectors.embedder
            for chunk_id, text in documents:
                vector = vectors.vector(text)
                if vector is not None:
                    self._dense[chunk_id] = vector
        elif vectors is not None:
            self.embedder_name = "given"
            self._dense = {cid: [float(v) for v in vec] for cid, vec in vectors.items()}
        self.dense_missing = [cid for cid, _ in documents if cid not in self._dense]
        self.name = HYBRID if self.hybrid_ready else BM25

    @property
    def hybrid_ready(self) -> bool:
        """True when every chunk has a vector, so a dense ranking covers the whole corpus."""
        return bool(self._chunks) and self.embedder_name is not None and not self.dense_missing

    @classmethod
    def from_directory(
        cls,
        knowledge_dir: Path,
        *,
        root: Path | None = None,
        vectors_path: Path | None = None,
        min_dense_similarity: float = 0.0,
        candidate_pool: int = CANDIDATE_POOL,
        overlays: Sequence[Path] = (),
    ) -> KnowledgeRetriever:
        """Build a retriever over a folder of Markdown runbooks (lexical reranker on).

        `overlays` are extra folders indexed with it (a red-team poisoned runbook, say);
        their chunks have no stored vector, so hybrid search is then not ready.

        With `vectors_path` (an existing vector file), hybrid search becomes possible for
        the chunks it covers; `hybrid_ready` says whether it covers all of them.
        """
        vectors = VectorIndex.load(vectors_path) if vectors_path is not None else None
        chunks = load_documents(knowledge_dir, root=root)
        for folder in overlays:  # extra folders; an overlay chunk has no stored vector
            chunks += load_documents(folder, root=root)
        return cls(
            chunks,
            reranker=LexicalReranker(),
            vectors=vectors,
            min_dense_similarity=min_dense_similarity,
            candidate_pool=candidate_pool,
        )

    @property
    def chunks(self) -> list[DocChunk]:
        """Every indexed chunk, in ingestion order (a copy, so callers cannot change the index)."""
        return list(self._chunks)

    def chunk(self, chunk_id: str) -> DocChunk | None:
        return self._by_id.get(chunk_id)

    def search(
        self, query: str, k: int = 4, query_vector: Sequence[float] | None = None
    ) -> list[RetrievedChunk]:
        """Top-k chunks for `query`, best first. Deterministic for the same index and query.

        Without `query_vector` (or when the dense index is incomplete) this is BM25 alone;
        with one, BM25 fused with the dense ranking. Each result says which in `retriever`.
        """
        if k <= 0 or not query.strip():
            return []
        pool = max(k, self.candidate_pool)
        hybrid = query_vector is not None and self.hybrid_ready
        lexical = self._bm25.search(query, pool)
        signals: dict[str, dict[str, float]] = {}
        for rank, (chunk_id, score) in enumerate(lexical, start=1):
            signals[chunk_id] = {"bm25_rank": rank, "bm25": round(score, 4)}
        candidates = lexical
        if hybrid and query_vector is not None:
            dense = self._dense_search(query_vector, pool)
            for rank, (chunk_id, similarity) in enumerate(dense, start=1):
                found = signals.setdefault(chunk_id, {})
                found.update({"dense_rank": rank, "cosine": round(similarity, 4)})
            fused = reciprocal_rank_fusion([[cid for cid, _ in lexical], [cid for cid, _ in dense]])
            for chunk_id, score in fused:
                signals[chunk_id]["rrf"] = round(score, 6)
            candidates = fused[:pool]
        results = [
            RetrievedChunk(
                chunk=self._by_id[chunk_id],
                score=score,
                rank=rank,
                retriever=HYBRID if hybrid else BM25,
                signals=signals.get(chunk_id, {}),
            )
            for rank, (chunk_id, score) in enumerate(candidates, start=1)
        ]
        if self._reranker is not None:
            results = self._reranker.rerank(query, results)
        return results[:k]

    def _dense_search(self, query_vector: Sequence[float], pool: int) -> list[tuple[str, float]]:
        """Chunks ranked by cosine similarity to the query, at or above the cutoff only."""
        scored = [
            (chunk.chunk_id, cosine(query_vector, self._dense[chunk.chunk_id]))
            for chunk in self._chunks
        ]
        scored = [item for item in scored if item[1] >= self.min_dense_similarity and item[1] > 0]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:pool]


def searchable_text(chunk: DocChunk) -> str:
    """What the indexes see (and what was embedded) for one chunk: its heading, then its text."""
    return f"{chunk.heading}\n{chunk.text}"
