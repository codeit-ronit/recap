"""Knowledge overlays (RECAP_KNOWLEDGE_OVERLAYS): extra runbook folders for one process.

An overlay is opt-in red-team or local material (a poisoned runbook, say). It may live in
the repository or anywhere else; what a run records about it names the folder, never the
local folders above it. Chunk IDs come from file names, so an overlay file named like a
shipped runbook is refused with a message naming both, instead of a crash at startup.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from recap.core.config import REPO_ROOT, Settings, load_settings
from recap.core.errors import ConfigError
from recap.core.models import RunConfig
from recap.knowledge.retriever import KnowledgeRetriever
from recap.runtime import Runtime
from tests.llm_fakes import OperatorApproves

KNOWLEDGE_DIR = REPO_ROOT / "fixtures" / "knowledge"
POISONED = REPO_ROOT / "fixtures" / "knowledge-overlays" / "poisoned"
RUNBOOK = "# Runbook: heap trouble\n\n## Symptoms\n\nOOMKilled pods restart again and again.\n"


def overlay(base: Path, name: str = "redteam-kb", file: str = "runbook-heap-trouble.md") -> Path:
    """An overlay folder outside the repository, holding one runbook."""
    folder = base / "somewhere" / "private" / name
    folder.mkdir(parents=True)
    (folder / file).write_text(RUNBOOK, encoding="utf-8")
    return folder


def test_an_overlay_outside_the_repository_loads_and_shows_only_its_own_name(
    tmp_path: Path,
) -> None:
    folder = overlay(tmp_path)
    retriever = KnowledgeRetriever.from_directory(KNOWLEDGE_DIR, root=REPO_ROOT, overlays=[folder])
    [chunk] = [c for c in retriever.chunks if c.chunk_id.startswith("runbook-heap-trouble#")]
    assert chunk.doc_path == "redteam-kb/runbook-heap-trouble.md"
    assert chunk.provenance["source"] == "redteam-kb"
    # No local folder above the overlay is on record (they can name a person or a project).
    assert "somewhere" not in chunk.doc_path + chunk.provenance["source"]
    assert retriever.search("OOMKilled pods restart again", k=1)[0].chunk == chunk


def test_overlay_folders_are_resolved_wherever_they_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absolute, repo-relative, or relative and climbing out of the repository with '..'."""
    folder = overlay(tmp_path)
    climbing = os.path.relpath(folder, REPO_ROOT)
    assert climbing.startswith("..")
    listed = [str(folder), climbing, "fixtures/knowledge-overlays/poisoned"]
    monkeypatch.setenv("RECAP_KNOWLEDGE_OVERLAYS", os.pathsep.join(listed))
    settings = load_settings(data_dir=tmp_path / "data")
    assert settings.paths.knowledge_overlays == [folder.resolve(), folder.resolve(), POISONED]


async def test_a_run_starts_with_an_overlay_outside_the_repository(
    settings: Settings, tmp_path: Path
) -> None:
    folder = overlay(tmp_path)
    paths = settings.paths.model_copy(update={"knowledge_overlays": [folder.resolve()]})
    runtime = Runtime(settings.model_copy(update={"paths": paths}))
    assert any(c.chunk_id.startswith("runbook-heap-trouble#") for c in runtime.knowledge.chunks)

    record = await runtime.run_incident(
        "bad-deployment", RunConfig(), approval_channel=OperatorApproves(runtime.approvals)
    )
    assert record.versions["knowledge_overlays"] == ["redteam-kb"]


def test_an_overlay_file_named_like_a_shipped_runbook_is_refused_naming_both(
    tmp_path: Path,
) -> None:
    folder = overlay(tmp_path, file="runbook-bad-deployment.md")
    with pytest.raises(ConfigError) as refused:
        KnowledgeRetriever.from_directory(KNOWLEDGE_DIR, root=REPO_ROOT, overlays=[folder])
    message = str(refused.value)
    assert "redteam-kb/runbook-bad-deployment.md" in message
    assert "fixtures/knowledge/runbook-bad-deployment.md" in message
    assert "rename" in message


def test_the_same_overlay_listed_twice_is_refused_too() -> None:
    with pytest.raises(ConfigError, match="runbook-checkout-heap.md"):
        KnowledgeRetriever.from_directory(
            KNOWLEDGE_DIR, root=REPO_ROOT, overlays=[POISONED, POISONED]
        )
