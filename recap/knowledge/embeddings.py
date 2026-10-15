"""Optional dense embeddings for hybrid retrieval (off by default).

BM25 only matches exact words, so a query about "heap exhaustion" misses a runbook that
says "out of memory". An embedding model maps texts to vectors whose closeness reflects
meaning, which closes that gap. `KnowledgeRetriever` can fuse such a dense ranking with
the BM25 ranking using reciprocal rank fusion (RRF).

This is a feature flag (RECAP_ENABLE_EMBEDDINGS / `enable_embeddings=True`). The default
demo never imports sentence-transformers and never downloads a model. When the flag is on,
the model is fetched from the Hugging Face hub on first use and then runs on CPU.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

from recap.core.errors import ConfigError

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # small (~90 MB), CPU-friendly
INSTALL_HINT = "pip install -e '.[embeddings]'"


class Embedder(Protocol):
    """Turns texts into dense vectors. Any model with this shape can plug in."""

    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """Embedder backed by the optional sentence-transformers package (CPU)."""

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        # Imported here, never at module import, so RECAP runs without the package.
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ConfigError(
                f"hybrid retrieval needs the optional embeddings extra: {INSTALL_HINT}"
            ) from exc
        self.name = model_name
        self._model = SentenceTransformer(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, as plain Python floats."""
        vectors = self._model.encode(texts, show_progress_bar=False)
        return [[float(value) for value in vector] for vector in vectors]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity: 1.0 = same direction, 0.0 = unrelated (or a zero vector)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Merge rankings: each id earns 1 / (k + rank) per list it appears in. Best first."""
    # RRF uses only rank positions, so BM25 scores and cosine similarities (which live on
    # different scales) never have to be normalized against each other. k = 60 is the
    # constant from the original RRF paper (Cormack et al., 2009); it keeps one list's
    # top hit from dominating the fused order.
    fused: dict[str, float] = {}
    for ranking in rankings:
        # dict.fromkeys drops repeats, so an id is credited once per list (its best rank).
        for rank, item_id in enumerate(dict.fromkeys(ranking), start=1):
            fused[item_id] = fused.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))
