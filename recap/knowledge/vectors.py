"""Document vectors for hybrid retrieval: computed once, stored, checked against the corpus.

Embedding the runbooks needs the embedding model; a search only needs its query embedded.
So the chunk vectors are computed once (scripts/build_vector_index.py, with the local
model) and stored in a file that CI and replays read without any model:

    fixtures/knowledge/vectors/nomic-embed-text.json
    {"schema": "recap-vectors-v1", "embedder": "ollama:nomic-embed-text", "dimensions": 768,
     "document_prefix": "search_document: ", "weights": "sha256:...", "created_at": "...",
     "vectors": {"<16 hex: sha256 of what was embedded>": "<base64 float32, little-endian>"}}

Each vector is keyed by a hash of exactly the text that was embedded (prefix included), so
an edited runbook section simply has no vector: the index then reports itself incomplete,
and RECAP searches with BM25 alone and says why, instead of silently using a stale vector.
Base64 float32 keeps the file small, and a vector reads back bit for bit the same.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from recap.core.errors import ConfigError
from recap.core.ids import utcnow
from recap.knowledge.embeddings import Embedder

SCHEMA = "recap-vectors-v1"
BATCH = 16  # texts per embedding request while building


class VectorIndex:
    """Vectors of embedded texts for one embedder, keyed by what was embedded."""

    def __init__(
        self,
        embedder: str,
        dimensions: int,
        document_prefix: str,
        vectors: Mapping[str, Sequence[float]],
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        self.embedder = embedder
        self.dimensions = dimensions
        self.document_prefix = document_prefix
        self.meta = dict(meta or {})
        self._vectors = {key: _float32(vector) for key, vector in vectors.items()}
        bad = [key for key, vector in self._vectors.items() if len(vector) != dimensions]
        if bad:
            raise ConfigError(f"{len(bad)} vectors do not have {dimensions} dimensions")

    def __len__(self) -> int:
        return len(self._vectors)

    def key(self, text: str) -> str:
        """The key of `text`: a hash of exactly what the embedder was given."""
        return hashlib.sha256((self.document_prefix + text).encode("utf-8")).hexdigest()[:16]

    def vector(self, text: str) -> list[float] | None:
        """The stored vector of `text`, or None if it was never embedded (or changed since)."""
        return self._vectors.get(self.key(text))

    def missing(self, texts: Iterable[str]) -> list[str]:
        """The texts this index has no vector for."""
        return [text for text in texts if self.key(text) not in self._vectors]

    @classmethod
    def load(cls, path: Path) -> VectorIndex:
        """Read a vector file. A missing or malformed file is a ConfigError, never a guess."""
        if not path.is_file():
            raise ConfigError(f"vector index not found: {path} (scripts/build_vector_index.py)")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"{path.name} is not a valid vector index ({exc})") from None
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ConfigError(f"{path}: not a {SCHEMA} file")
        dimensions = int(data["dimensions"])
        try:
            vectors = {key: _decode(blob, dimensions) for key, blob in data["vectors"].items()}
        except (ValueError, struct.error, TypeError) as exc:
            raise ConfigError(f"{path.name}: unreadable vector ({exc})") from None
        meta = {k: v for k, v in data.items() if k not in ("vectors", "schema")}
        return cls(str(data["embedder"]), dimensions, str(data["document_prefix"]), vectors, meta)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema": SCHEMA,
            **self.meta,
            "embedder": self.embedder,
            "dimensions": self.dimensions,
            "document_prefix": self.document_prefix,
            "vectors": {key: _encode(self._vectors[key]) for key in sorted(self._vectors)},
        }
        path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    async def build(
        cls,
        texts: Sequence[str],
        embedder: Embedder,
        *,
        dimensions: int,
        document_prefix: str,
        meta: Mapping[str, Any] | None = None,
    ) -> VectorIndex:
        """Embed every text (in batches) with `embedder` and keep the vectors."""
        unique = list(dict.fromkeys(texts))
        vectors: list[list[float]] = []
        for start in range(0, len(unique), BATCH):
            vectors += await embedder.embed_documents(unique[start : start + BATCH])
        if len(vectors) != len(unique):
            raise ConfigError(f"{embedder.name} returned {len(vectors)} vectors for {len(unique)}")
        draft = cls(embedder.name, dimensions, document_prefix, {}, meta)
        keyed = {draft.key(text): vector for text, vector in zip(unique, vectors, strict=True)}
        info = {"created_at": utcnow().isoformat(), **(meta or {})}
        return cls(embedder.name, dimensions, document_prefix, keyed, info)


def _float32(vector: Sequence[float]) -> list[float]:
    """The vector as float32 values: exactly what a stored vector reads back as."""
    values = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in values):
        raise ConfigError("a vector holds a value that is not a finite number")
    return list(struct.unpack(f"<{len(values)}f", struct.pack(f"<{len(values)}f", *values)))


def _encode(vector: Sequence[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def _decode(blob: str, dimensions: int) -> list[float]:
    raw = base64.b64decode(blob, validate=True)
    if len(raw) != 4 * dimensions:
        raise ValueError(f"{len(raw)} bytes, expected {4 * dimensions}")
    return list(struct.unpack(f"<{dimensions}f", raw))
