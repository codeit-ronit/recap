"""Committed model recordings (fixtures/recordings/): what each one is, with its bytes checked.

    fixtures/recordings/MANIFEST.json --> list_fixtures() --> the dashboard's replay launcher
    RunConfig.replay_fixture (an id)  --> fixture_path()  --> the replay provider's file

A recording is content-addressed: the manifest pins each file's SHA-256, and a file whose
bytes differ is refused (ConfigError), so a replay never runs from an altered fixture. A
client names a recording only by its manifest id, never by a path. After re-recording a
fixture, the owner refreshes its entry with `python scripts/recordings_manifest.py --update`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import Field

from recap.core.config import REPO_ROOT
from recap.core.errors import ConfigError
from recap.core.models import Contract

RECORDINGS_DIR = REPO_ROOT / "fixtures" / "recordings"
MANIFEST = RECORDINGS_DIR / "MANIFEST.json"
SCHEMA = "recap-recordings-manifest-v1"


class FixtureInfo(Contract):
    """One committed recording: its file, its hash, and the run it reproduces."""

    id: str
    file: str  # a file name inside fixtures/recordings/
    sha256: str
    bytes: int
    recording_id: str
    title: str
    description: str = ""
    scenario: str
    strategy: str = "adaptive"  # how the original run was routed
    use_memory: bool = False
    retrieval: str = "bm25"
    provider: str  # the provider that made it (e.g. ollama)
    model: str
    prompt_version: str
    agents: str = ""  # e.g. "two specialists in parallel, two rounds"
    approved_by: str = ""  # who approved the original fix (the replay asks again)
    exported_run: str | None = None  # repo-relative folder in evals/live/, when published
    tags: list[str] = Field(default_factory=list)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def list_fixtures(manifest: Path = MANIFEST) -> list[FixtureInfo]:
    """The manifest's recordings, in manifest order (an empty list when there is none)."""
    if not manifest.is_file():
        return []
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ConfigError(f"{manifest}: not a {SCHEMA} file")
    return [FixtureInfo.model_validate(item) for item in data.get("fixtures", [])]


def fixture_path(fixture_id: str, manifest: Path = MANIFEST) -> Path:
    """The file of recording `fixture_id`, after checking its bytes against the manifest."""
    for info in list_fixtures(manifest):
        if info.id == fixture_id:
            path = manifest.parent / info.file
            if Path(info.file).name != info.file or not path.is_file():
                raise ConfigError(f"recording {fixture_id!r}: {info.file} is missing")
            actual = sha256_of(path)
            if actual != info.sha256:
                raise ConfigError(
                    f"recording {fixture_id!r} was altered: {info.file} has SHA-256 "
                    f"{actual[:12]}..., the manifest pins {info.sha256[:12]}... "
                    "(re-record it, or refresh the manifest if the change is intended)"
                )
            return path
    raise ConfigError(f"unknown recording {fixture_id!r} (see fixtures/recordings/MANIFEST.json)")
