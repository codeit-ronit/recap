"""scripts/build_vector_index.py --check: are the stored vectors current for the configured
embedder? The same comparison a hybrid run makes (VectorIndex.mismatch): the model, the
dimensions, the document prefix and, when the local server answers, the weights.

Offline: a fake Ollama server stands in for the local one, and --check writes nothing.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from recap.core.config import REPO_ROOT, load_settings
from recap.providers.transport import LiveTransport
from scripts import build_vector_index
from tests import llm_fakes
from tests.llm_fakes import FakeOllama

VECTORS = REPO_ROOT / "fixtures" / "knowledge" / "vectors" / "nomic-embed-text.json"
STORED_WEIGHTS = json.loads(VECTORS.read_text(encoding="utf-8"))["weights"]


def serve(monkeypatch: pytest.MonkeyPatch, mock: httpx.MockTransport) -> None:
    """Every transport the script opens talks to `mock` instead of the local server."""

    def transport(base_url: str) -> LiveTransport:
        return LiveTransport(base_url, mock=mock)

    monkeypatch.setattr(build_vector_index, "LiveTransport", transport)


@pytest.fixture
def same_weights(monkeypatch: pytest.MonkeyPatch) -> FakeOllama:
    """A local server whose nomic-embed-text has the weights the vectors were made with."""
    monkeypatch.setattr(llm_fakes, "FAKE_EMBED_WEIGHTS", STORED_WEIGHTS.replace(":", "-"))
    fake = FakeOllama()
    serve(monkeypatch, fake.mock())
    return fake


def configure(monkeypatch: pytest.MonkeyPatch, **embedder: Any) -> None:
    """The repository's settings, with another embedder configured in retrieval.yaml."""
    settings = load_settings()
    spec = settings.retrieval.embedder.model_copy(update=embedder)
    changed = settings.model_copy(
        update={"retrieval": settings.retrieval.model_copy(update={"embedder": spec})}
    )
    monkeypatch.setattr(build_vector_index, "load_settings", lambda: changed)


async def test_the_stored_vectors_are_current(
    same_weights: FakeOllama, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await build_vector_index.main(check_only=True) == 0
    assert "is current" in capsys.readouterr().out
    assert same_weights.embeds == []  # checking embeds nothing


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"document_prefix": "passage: "}, "'passage: '"),
        ({"model": "granite-embedding"}, "ollama:granite-embedding"),
        ({"dimensions": 1024}, "1024 dimensions"),
    ],
)
async def test_vectors_made_for_another_embedder_are_stale(
    same_weights: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
    named: str,
) -> None:
    configure(monkeypatch, **change)
    assert await build_vector_index.main(check_only=True) == 1
    out = capsys.readouterr().out
    assert out.startswith("stale:") and named in out


async def test_vectors_made_from_other_weights_are_stale(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(llm_fakes, "FAKE_EMBED_WEIGHTS", "sha256-" + "e3be" * 16)
    serve(monkeypatch, FakeOllama().mock())  # other weights than the stored vectors'
    assert await build_vector_index.main(check_only=True) == 1
    out = capsys.readouterr().out
    assert out.startswith("stale:") and "weights" in out


async def test_checking_without_a_server_compares_everything_but_the_weights(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    serve(monkeypatch, httpx.MockTransport(refuse))
    assert await build_vector_index.main(check_only=True) == 0
    out = capsys.readouterr().out
    assert "is current" in out and "weights not compared" in out
