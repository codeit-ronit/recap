"""Embed every runbook chunk with the local embedding model and store the vectors.

    python scripts/build_vector_index.py          # writes the file configs/retrieval.yaml names
    python scripts/build_vector_index.py --check  # only report whether the stored file is current

Hybrid retrieval needs a vector for every chunk. Searching then needs only the query
embedded, so CI and replays work from the stored file without any model. Run this again
whenever a runbook in fixtures/knowledge/ changes (a test fails while the file is stale),
or when configs/retrieval.yaml names another embedding model, dimensions or prefix.

The stored file is current when it has a vector for every chunk AND fits the configured
embedder, the same comparison a hybrid run makes (VectorIndex.mismatch): the model, its
dimensions, the document prefix and, when the local server answers, its weights (asked
for as metadata; nothing is embedded by --check). Without a server, the weights are not
compared and the output says so.

Local only: it talks to Ollama on loopback (nomic-embed-text must already be installed by
the owner; RECAP never downloads models). Every request is free and stays on this machine.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from recap.core.config import load_settings
from recap.core.errors import ConfigError, ProviderError
from recap.knowledge.ingest import load_documents
from recap.knowledge.ollama_embedder import OllamaEmbedder
from recap.knowledge.retriever import searchable_text
from recap.knowledge.vectors import VectorIndex
from recap.providers.transport import LiveTransport


async def main(check_only: bool) -> int:
    settings = load_settings()
    spec = settings.retrieval.embedder
    path = settings.paths.root / spec.vectors
    chunks = load_documents(settings.paths.knowledge_dir, root=settings.paths.root)
    texts = [searchable_text(chunk) for chunk in chunks]
    embedder = OllamaEmbedder(spec, LiveTransport(spec.base_url))

    if path.is_file():
        stored = VectorIndex.load(path)
        unchecked = await _ask_for_weights(embedder)
        problem = stored.mismatch(embedder)  # vectors of another embedder: every one is stale
        missing = texts if problem else stored.missing(texts)
        if not missing and len(stored) == len(set(texts)):
            shown = path.relative_to(settings.paths.root)
            print(f"{shown} is current ({len(texts)} chunks){unchecked}")
            return 0
        print(f"stale: {problem or f'{len(missing)} of {len(texts)} chunks have no vector'}")
    elif check_only:
        print(f"missing: {path}")
    if check_only:
        return 1

    try:
        server = await embedder.ensure_ready()
        index = await VectorIndex.build(
            texts,
            embedder,
            dimensions=spec.dimensions,
            document_prefix=spec.document_prefix,
            meta={
                "model": spec.model,
                "weights": server.get("weights"),
                "ollama_version": server.get("ollama_version"),
                "chunks": len(texts),
            },
        )
    except (ProviderError, ConfigError) as exc:
        print(f"could not build the vectors: {exc}", file=sys.stderr)
        return 2
    index.save(path)
    print(f"wrote {len(index)} vectors to {path.relative_to(settings.paths.root)} ({spec.model})")
    return 0


async def _ask_for_weights(embedder: OllamaEmbedder) -> str:
    """Ask the local server which weights it has (metadata only: nothing is embedded), so
    the stored file's can be compared with them. Returns why they could not be, or ""."""
    try:
        await embedder.ensure_ready()
    except ProviderError as exc:
        return f" (weights not compared: {exc})"
    return ""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report only; write nothing")
    sys.exit(asyncio.run(main(parser.parse_args().check)))
