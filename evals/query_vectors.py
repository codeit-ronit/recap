"""Stored query vectors: hybrid retrieval in an evaluation without a model server (M6 W4).

Hybrid search embeds each runbook query with nomic-embed-text. The scripted evaluation's
queries are deterministic (built from the alert and the proposed fixes, never by a model),
so they are embedded once with the local model and stored, in the runbook vectors' format
(recap/knowledge/vectors.py, keyed by a hash of exactly what was embedded):

    evals/datasets/query-vectors-v1.json

An evaluation then answers every query from that file, and refuses a query it has no
vector for: a changed query means the stored set no longer covers the runs, so it is
captured again (scripts/evaluate.py --capture-query-vectors), never approximated.

Capturing needs Ollama with nomic-embed-text, briefly, through the same local-only embedder
a run would use (create_embedder). The stored vectors carry the server's weights digest,
and a run's hybrid search compares it with the runbook vectors' (the controller refuses
vectors from other weights), so a re-pulled model cannot silently change a result.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from recap.core.config import EmbedderSettings
from recap.core.errors import ConfigError
from recap.core.ids import utcnow
from recap.knowledge.embeddings import Embedder
from recap.knowledge.vectors import VectorIndex

KIND = "queries"  # what the file's vectors embed (its prefix is the query prefix)


class QueryVectors:
    """The stored query vectors of one evaluation, and (while capturing) the new ones.

    `live` is the local embedder, given only while capturing: a query without a stored
    vector is then embedded live and kept for `save`. Without it, such a query is refused.
    """

    def __init__(
        self, index: VectorIndex, settings: EmbedderSettings, *, live: Embedder | None = None
    ) -> None:
        problems = []
        if index.embedder != f"ollama:{settings.model}":
            problems.append(f"they were made by {index.embedder}, not ollama:{settings.model}")
        if index.dimensions != settings.dimensions:
            problems.append(f"they have {index.dimensions} dimensions, not {settings.dimensions}")
        if index.document_prefix != settings.query_prefix:
            problems.append(
                f"they were embedded with the prefix {index.document_prefix!r}, not the query "
                f"prefix {settings.query_prefix!r}"
            )
        if index.meta.get("kind", KIND) != KIND:
            problems.append(f"the file holds {index.meta.get('kind')!r} vectors, not queries")
        if problems:
            raise ConfigError("the stored query vectors cannot be used: " + "; ".join(problems))
        self.index = index
        self.settings = settings
        self.live = live
        self.captured: dict[str, list[float]] = {}
        self.missing: list[str] = []  # queries asked for that the file lacks (stored mode)
        self.weights: str | None = index.meta.get("weights")
        self.ollama_version: str | None = index.meta.get("ollama_version")

    @classmethod
    def load(cls, path: Path, settings: EmbedderSettings) -> QueryVectors:
        """Stored vectors; refused without a weights digest (nothing could prove they fit the
        runbook vectors, so a hybrid search over them could compare two models' vectors)."""
        found = cls(VectorIndex.load(path), settings)
        if not found.weights:
            raise ConfigError(
                f"{path.name} names no weights digest: nothing proves its vectors fit the "
                "runbook vectors; capture them again"
            )
        return found

    @classmethod
    def empty(
        cls, settings: EmbedderSettings, live: Embedder, *, weights: str | None = None
    ) -> QueryVectors:
        """A new file's contents, to be filled by a capture. `weights`: the runbook vectors'
        weights digest, so a server with other weights is refused at the first query."""
        index = VectorIndex(
            f"ollama:{settings.model}", settings.dimensions, settings.query_prefix, {}, {}
        )
        found = cls(index, settings, live=live)
        found.weights = weights
        return found

    def capturing(self, live: Embedder) -> QueryVectors:
        """The same stored vectors, now able to embed (and keep) missing queries live."""
        self.live = live
        return self

    def embedder(self) -> StoredQueryEmbedder:
        """A run's embedder over these vectors (one per run: the run sets its `on_call`)."""
        return StoredQueryEmbedder(self)

    def lookup(self, text: str) -> list[float] | None:
        stored = self.index.vector(text)
        return stored if stored is not None else self.captured.get(text)

    async def capture(self, text: str) -> list[float]:
        if self.live is None:
            self.missing.append(text)
            raise ConfigError(
                f"no stored query vector for {_preview(text)}: the stored set does not cover "
                "this evaluation's queries; capture them again with Ollama running "
                "(scripts/evaluate.py --capture-query-vectors)"
            )
        vector = await self.live.embed_query(text)
        server = getattr(self.live, "server", None) or {}
        weights = server.get("weights")
        if not weights:
            raise ConfigError(
                "the embedding server reported no weights digest: nothing would prove these "
                "vectors fit the runbook vectors, so none is kept"
            )
        if self.weights and weights != self.weights:
            raise ConfigError(
                f"the stored query vectors come from the weights {self.weights}, the running "
                f"server has {weights}: capture into a new file instead of mixing them"
            )
        self.weights = self.weights or weights
        self.ollama_version = self.ollama_version or server.get("ollama_version")
        self.captured[text] = vector
        return vector

    def save(self, path: Path) -> int:
        """Write the stored and captured vectors to `path`; return how many were added."""
        draft = self.index
        added = len(draft.missing(self.captured))
        queries = sorted({*draft.meta.get("queries", []), *self.captured})
        meta: dict[str, Any] = {
            **draft.meta,
            "kind": KIND,
            "model": self.settings.model,
            "query_prefix": self.settings.query_prefix,
            "weights": self.weights,
            "ollama_version": self.ollama_version,
            "created_at": draft.meta.get("created_at") or utcnow().isoformat(),
            "updated_at": utcnow().isoformat(),
            # What was embedded, readable: every query text of the evaluation's runs.
            "queries": queries,
        }
        draft.extended(self.captured, meta).save(path)
        return added


class StoredQueryEmbedder:
    """An Embedder that answers queries from stored vectors and never contacts a server.

    It describes itself like the OllamaEmbedder that made the vectors (name, dimensions,
    the runbooks' document prefix, the server's weights), so the controller's check that
    query and runbook vectors fit together applies to it unchanged.
    """

    def __init__(self, vectors: QueryVectors) -> None:
        settings = vectors.settings
        self.vectors = vectors
        self.name = f"ollama:{settings.model}"
        self.dimensions = settings.dimensions
        self.document_prefix = settings.document_prefix
        self.query_prefix = settings.query_prefix
        self.on_call: Callable[[dict[str, Any]], None] = lambda call: None

    @property
    def server(self) -> dict[str, Any] | None:
        weights = self.vectors.weights
        if not weights:
            return None
        return {"weights": weights, "ollama_version": self.vectors.ollama_version, "stored": True}

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise ConfigError("stored query vectors embed queries only (scripts/build_vector_index.py)")

    async def embed_query(self, text: str) -> list[float]:
        vector = self.vectors.lookup(text)
        stored = vector is not None
        if vector is None:
            vector = await self.vectors.capture(text)
        self.on_call(
            {
                "model": self.vectors.settings.model,
                "purpose": "query",
                "inputs": 1,
                "status": 200,
                "input_tokens": None,
                "duration_ms": 0 if stored else None,
                "replayed": stored,  # answered from the stored file, not by a server
            }
        )
        return vector


def _preview(text: str) -> str:
    shown = " ".join(text.split())
    return repr(shown if len(shown) <= 80 else shown[:77] + "...")
