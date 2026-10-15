"""Knowledge retriever: the search entry point of RECAP's static RAG.

The controller asks `KnowledgeRetriever.search(query)` for runbook chunks during the
RETRIEVING stage and records the cited results. Retrieval runs in two stages:

    1. Candidates: BM25 keyword ranking (bm25.py). With the embeddings feature flag on,
       a dense cosine ranking is fused in with reciprocal rank fusion (embeddings.py).
    2. Rerank: a `Reranker` re-orders the candidates. V1 ships `LexicalReranker`, a
       simple heuristic; the interface lets a real neural reranker plug in later.

Retrieved text is UNTRUSTED data. This module ranks and cites it but never acts on it:
a poisoned runbook comes back verbatim, and the trusted safety policy downstream decides
what may actually run.
"""

from __future__ import annotations

from pathlib import Path

from recap.core.interfaces import Reranker
from recap.core.models import DocChunk, RetrievedChunk
from recap.knowledge.bm25 import BM25Index, tokenize
from recap.knowledge.embeddings import (
    Embedder,
    SentenceTransformerEmbedder,
    cosine,
    reciprocal_rank_fusion,
)
from recap.knowledge.ingest import load_documents

CANDIDATE_POOL = 20  # first-stage candidates handed to the reranker (at least k)
HEADING_BOOST = 0.25  # max score boost when every query word appears in the heading/title


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
    """Searches runbook chunks and returns ranked, cited `RetrievedChunk`s (RetrievalIndex)."""

    def __init__(
        self,
        chunks: list[DocChunk],
        *,
        reranker: Reranker | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._by_id = {chunk.chunk_id: chunk for chunk in self._chunks}
        self._reranker = reranker
        self._embedder = embedder
        # The heading is indexed with the text: "Remediation" is a useful search word.
        documents = [(chunk.chunk_id, _searchable_text(chunk)) for chunk in self._chunks]
        self._bm25 = BM25Index(documents)  # raises ValueError on duplicate chunk ids
        # Chunk vectors are computed once here; only the query is embedded per search.
        texts = [text for _, text in documents]
        self._vectors = embedder.embed(texts) if embedder is not None and texts else []
        self.name = "hybrid-bm25-embeddings" if embedder is not None else "bm25"

    @classmethod
    def from_directory(
        cls, knowledge_dir: Path, *, root: Path | None = None, enable_embeddings: bool = False
    ) -> KnowledgeRetriever:
        """Build a retriever over a folder of Markdown runbooks (lexical reranker on).

        `enable_embeddings=True` turns on hybrid retrieval; it raises ConfigError if the
        optional sentence-transformers package is not installed.
        """
        embedder = SentenceTransformerEmbedder() if enable_embeddings else None
        return cls(
            load_documents(knowledge_dir, root=root), reranker=LexicalReranker(), embedder=embedder
        )

    @property
    def chunks(self) -> list[DocChunk]:
        """Every indexed chunk, in ingestion order (a copy, so callers cannot change the index)."""
        return list(self._chunks)

    def search(self, query: str, k: int = 4) -> list[RetrievedChunk]:
        """Top-k chunks for `query`, best first. Deterministic for the same index and query."""
        if k <= 0 or not query.strip():
            return []
        candidates = self._candidates(query, pool=max(k, CANDIDATE_POOL))
        results = [
            RetrievedChunk(chunk=self._by_id[chunk_id], score=score, rank=rank, retriever=self.name)
            for rank, (chunk_id, score) in enumerate(candidates, start=1)
        ]
        if self._reranker is not None:
            results = self._reranker.rerank(query, results)
        return results[:k]

    def _candidates(self, query: str, pool: int) -> list[tuple[str, float]]:
        """First stage: BM25 alone, or BM25 fused with the dense ranking (hybrid mode)."""
        lexical = self._bm25.search(query, pool)
        if self._embedder is None:
            return lexical
        dense = self._dense_search(self._embedder, query, pool)
        fused = reciprocal_rank_fusion([[cid for cid, _ in lexical], [cid for cid, _ in dense]])
        return fused[:pool]

    def _dense_search(self, embedder: Embedder, query: str, pool: int) -> list[tuple[str, float]]:
        """Chunks ranked by cosine similarity to the query vector (positive similarity only)."""
        query_vector = embedder.embed([query])[0]
        scored = [
            (chunk.chunk_id, cosine(query_vector, vector))
            for chunk, vector in zip(self._chunks, self._vectors, strict=True)
        ]
        scored = [item for item in scored if item[1] > 0]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:pool]


def _searchable_text(chunk: DocChunk) -> str:
    """What the indexes see for one chunk: its heading followed by its text."""
    return f"{chunk.heading}\n{chunk.text}"
