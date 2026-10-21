"""nomic-embed-text through Ollama, on this machine only: hybrid retrieval's embedder.

Where it sits:

    controller (initial and proposal retrieval) and the search_runbooks tool
        --> OllamaEmbedder.embed_query("checkout HTTP 5xx ...")  --> one query vector
    scripts/build_vector_index.py
        --> OllamaEmbedder.embed_documents([...])                --> the stored chunk vectors

It speaks Ollama's /api/embed through a ModelTransport (recap/providers/transport.py), so
a run's embedding requests are recorded with its model exchanges and replay offline, just
like chat requests. The chat provider's local-only rules hold here too: loopback only, no
proxy (the transport ignores proxy settings), no cloud model, and RECAP never downloads
the model (`ollama pull nomic-embed-text` is the owner's decision).

nomic-embed-text was trained with task prefixes: "search_document: " on what is indexed and
"search_query: " on what is searched for. Its vectors have 768 dimensions.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from recap.core.config import EmbedderSettings
from recap.core.errors import ConfigError, ProviderError
from recap.providers.transport import ModelTransport

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
PROBE_TIMEOUT_SECONDS = 10.0
KEEP_ALIVE = "10m"  # keep the model loaded between the searches of a run

# What one embedding request reports, for the trace (`retrieval.embed`) and the run record.
EmbedCall = dict[str, Any]


def check_local_embedder(settings: EmbedderSettings) -> None:
    """Refuse a non-loopback server or a cloud-named model before anything is sent."""
    host = urlsplit(settings.base_url).hostname or ""
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"the Ollama embedder is local-only: base_url must be on {', '.join(LOOPBACK_HOSTS)}, "
            f"not {host or settings.base_url!r}"
        )
    model = settings.model.lower()
    if model.split(":")[-1].endswith("cloud") or model.endswith("-cloud"):
        raise ConfigError(f"{settings.model!r} is an Ollama cloud model: RECAP refuses it")


class OllamaEmbedder:
    """An Embedder (recap/knowledge/embeddings.py) backed by a local Ollama model."""

    def __init__(
        self,
        settings: EmbedderSettings,
        transport: ModelTransport,
        *,
        on_call: Callable[[EmbedCall], None] | None = None,
    ) -> None:
        check_local_embedder(settings)
        self.settings = settings
        self.transport = transport
        self.name = f"ollama:{settings.model}"
        self.document_prefix = settings.document_prefix
        self.query_prefix = settings.query_prefix
        self.dimensions = settings.dimensions
        # Called with each request's facts (model, tokens, time, replayed or live).
        self.on_call: Callable[[EmbedCall], None] = on_call or (lambda call: None)
        # What the server reported about the model (weights file, size), after the first check.
        self.server: dict[str, Any] | None = None
        self._ready_lock = asyncio.Lock()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([self.document_prefix + text for text in texts], "documents")

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([self.query_prefix + text], "query"))[0]

    async def ensure_ready(self) -> dict[str, Any]:
        """Check the server and that the model embeds (metadata only), once per embedder."""
        if self.server is not None:
            return self.server
        async with self._ready_lock:
            if self.server is None:
                self.server = await self._check_ready()
        return self.server

    async def _check_ready(self) -> dict[str, Any]:
        model = self.settings.model
        version = await self.transport.request("GET", "/api/version", timeout=PROBE_TIMEOUT_SECONDS)
        if version.status != 200:
            raise ProviderError(
                f"Ollama answered HTTP {version.status} at {self.settings.base_url}; start it "
                "with: OLLAMA_NO_CLOUD=1 ollama serve"
            )
        show = await self.transport.request(
            "POST", "/api/show", {"model": model}, timeout=PROBE_TIMEOUT_SECONDS
        )
        if show.status == 404:
            raise ProviderError(
                f"{model} is not installed in Ollama. RECAP never downloads models; once you "
                f"have decided to, install it yourself: ollama pull {model}"
            )
        body = show.body if isinstance(show.body, dict) else {}
        if show.status != 200:
            raise ProviderError(f"Ollama /api/show answered HTTP {show.status} for {model}")
        if body.get("remote_host") or body.get("remote_model"):
            raise ProviderError(f"{model} is served from a remote host: RECAP refuses it")
        capabilities = [str(c) for c in body.get("capabilities") or []]
        if "embedding" not in capabilities:
            raise ProviderError(f"{model} is not an embedding model (capabilities: {capabilities})")
        from recap.providers.ollama_provider import weights_digest  # same rule as for chat

        details = body.get("details") or {}
        return {
            "ollama_version": (version.body or {}).get("version"),
            "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            "weights": weights_digest(body),
        }

    async def _embed(self, inputs: list[str], purpose: str) -> list[list[float]]:
        if not inputs:
            return []
        await self.ensure_ready()
        body = {"model": self.settings.model, "input": inputs, "keep_alive": KEEP_ALIVE}
        exchange = await self.transport.request(
            "POST", "/api/embed", body, timeout=self.settings.request_timeout_seconds
        )
        data = exchange.body if isinstance(exchange.body, dict) else {}
        tokens = data.get("prompt_eval_count")
        self.on_call(
            {
                "model": self.settings.model,
                "purpose": purpose,
                "inputs": len(inputs),
                "status": exchange.status,
                "input_tokens": tokens if isinstance(tokens, int) else None,
                "duration_ms": exchange.duration_ms,
                "replayed": exchange.replayed,
            }
        )
        if exchange.status != 200:
            detail = str(data.get("error") or exchange.text)[:200]
            raise ProviderError(f"Ollama /api/embed answered HTTP {exchange.status}: {detail}")
        return [self._checked(vector) for vector in self._vectors(data, len(inputs))]

    def _vectors(self, data: dict[str, Any], expected: int) -> list[Any]:
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != expected:
            got = len(vectors) if isinstance(vectors, list) else "no"
            raise ProviderError(f"Ollama returned {got} embeddings for {expected} inputs")
        return vectors

    def _checked(self, vector: Any) -> list[float]:
        """A vector of the configured size made of finite numbers, or a ProviderError."""
        if not isinstance(vector, list) or len(vector) != self.dimensions:
            size = len(vector) if isinstance(vector, list) else "no"
            raise ProviderError(
                f"{self.name} returned a vector of {size} dimensions, expected {self.dimensions}"
            )
        values: list[float] = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ProviderError(f"{self.name} returned a vector with a non-number in it")
            if not math.isfinite(value):
                raise ProviderError(f"{self.name} returned a vector with a non-finite number")
            values.append(float(value))
        return values
