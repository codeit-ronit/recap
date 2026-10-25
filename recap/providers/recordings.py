"""Committed model recordings (fixtures/recordings/): what each one is, with its bytes checked.

    fixtures/recordings/MANIFEST.json --> list_fixtures()  --> the dashboard's replay launcher
    RunConfig.replay_fixture (an id)  --> read_fixture()   --> the replay provider (factory.py)
    any recording's bytes             --> fixture_for()    --> "these bytes ARE fixture X"
    scripts/recordings_manifest.py    --> check_manifest() and refresh_manifest()

A recording is content-addressed: the manifest pins each file's SHA-256, and a file whose
bytes differ is refused (ConfigError), so a replay never runs from an altered fixture.
read_fixture returns the very bytes it hashed and the replay parses those, so nothing can
change between the check and the replay. A client names a recording only by its manifest
id, never by a path.

Every entry is a recording of a REAL model run, never one made against a test's fake model
server: that is what lets the API label a replay of it a recorded real-LLM run (by the
replay's `replay_of["fixture"]`, which only these exact bytes earn). The manifest's prose
(title, description, agents, approved_by) is written by a person and checked in review; the
facts it repeats from the recording's own header (recording_id, provider, model,
prompt_version) and the pinned hash and size are checked by check_manifest, which CI runs
(tests/unit/test_recordings_manifest.py). After a deliberate re-recording the owner
refreshes the pins with `python scripts/recordings_manifest.py --update` and then reviews
the prose, which describes the run that was recorded.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import Field, ValidationError

from recap.core.config import REPO_ROOT
from recap.core.errors import ConfigError
from recap.core.models import Contract
from recap.providers.transport import Recording

RECORDINGS_DIR = REPO_ROOT / "fixtures" / "recordings"
MANIFEST = RECORDINGS_DIR / "MANIFEST.json"
SCHEMA = "recap-recordings-manifest-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")

# Facts a manifest entry repeats from its recording's header line: entry field -> header key.
HEADER_FACTS = {
    "recording_id": "recording_id",
    "provider": "original_provider",
    "model": "model",
    "prompt_version": "prompt_version",
}


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


class FixtureBytes(NamedTuple):
    """A committed recording after its check: the entry, the file and the bytes that matched."""

    info: FixtureInfo
    path: Path
    data: bytes


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def list_fixtures(manifest: Path | None = None) -> list[FixtureInfo]:
    """The manifest's recordings, in manifest order (an empty list when there is none).

    A manifest that is not valid JSON, has another schema, an invalid entry or two entries
    with one id is refused as a whole (ConfigError): an id must name exactly one recording.
    """
    path = _manifest(manifest)
    if not path.is_file():
        return []
    fixtures: list[FixtureInfo] = []
    for number, item in enumerate(_load(path).get("fixtures", []), start=1):
        try:
            info = FixtureInfo.model_validate(item)
        except ValidationError as exc:
            fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
            raise ConfigError(
                f"{path.name}: entry {number} is invalid (check {', '.join(fields)})"
            ) from None
        if any(known.id == info.id for known in fixtures):
            raise ConfigError(f"{path.name}: two entries have the id {info.id!r}")
        fixtures.append(info)
    return fixtures


def read_fixture(fixture_id: str, manifest: Path | None = None) -> FixtureBytes:
    """Recording `fixture_id`: its entry, its file and its bytes, checked against the pin.

    The bytes returned are the ones that were hashed: a caller that parses them (the replay
    provider) uses exactly what was checked, even if the file changes a moment later.
    """
    path = _manifest(manifest)
    for info in list_fixtures(path):
        if info.id != fixture_id:
            continue
        if not _is_file_name(info.file):
            raise ConfigError(
                f"recording {fixture_id!r}: {info.file!r} is not a file name in the "
                "manifest's folder"
            )
        file = path.parent / info.file
        if not file.is_file():
            raise ConfigError(f"recording {fixture_id!r}: {info.file} is missing")
        data = file.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != info.sha256:
            raise ConfigError(
                f"recording {fixture_id!r} was altered: {info.file} has SHA-256 "
                f"{actual[:12]}..., the manifest pins {info.sha256[:12]}... "
                "(re-record it, or refresh the manifest if the change is intended)"
            )
        return FixtureBytes(info, file, data)
    raise ConfigError(f"unknown recording {fixture_id!r} (see fixtures/recordings/MANIFEST.json)")


def fixture_path(fixture_id: str, manifest: Path | None = None) -> Path:
    """The file of recording `fixture_id`, after checking its bytes against the manifest."""
    return read_fixture(fixture_id, manifest).path


def fixture_for(digest: str, manifest: Path | None = None) -> FixtureInfo | None:
    """The committed recording whose pinned SHA-256 is `digest`, or None.

    Only the bytes decide (content addressing): a copy of a fixture anywhere is that
    fixture, and a file with one changed byte is not.
    """
    return next((info for info in list_fixtures(manifest) if info.sha256 == digest), None)


# --- the owner's checks (scripts/recordings_manifest.py) ----------------------------------


def check_manifest(manifest: Path | None = None) -> list[str]:
    """Every problem with the manifest and the folder it describes; [] when all is well.

    It checks that each entry names a file in the manifest's folder whose bytes match the
    pinned SHA-256 and size, whose header repeats the entry's facts, and whose export folder
    (if any) exists; and that every recording in the folder has an entry.
    """
    path = _manifest(manifest)
    if not path.is_file():
        return [f"{path.name} is missing"]
    try:
        fixtures = list_fixtures(path)
    except ConfigError as exc:
        return [str(exc)]
    problems: list[str] = []
    listed: set[str] = set()
    for info in fixtures:
        listed.add(info.file)
        problems += _entry_problems(info, path.parent)
    for file in sorted(path.parent.glob("*.jsonl")):
        if file.name not in listed:
            problems.append(f"{file.name} is not in {path.name}: add an entry for it by hand")
    return problems


def refresh_manifest(manifest: Path | None = None) -> list[str]:
    """After a deliberate re-recording: re-pin each listed file's SHA-256 and size, and copy
    the facts the entry repeats from the file's header. Returns what changed ([]: nothing,
    and the file is left byte for byte as it was).

    Only existing entries are refreshed; a new recording needs an entry written by hand
    (its title and description say what the run was). The prose of a refreshed entry is
    not touched: it still describes the earlier run until the owner reviews it.
    """
    path = _manifest(manifest)
    if not path.is_file():
        raise ConfigError(f"{path.name} is missing")
    data = _load(path)
    list_fixtures(path)  # refuse invalid entries and duplicate ids before changing anything
    fresh: list[dict[str, Any]] = []
    for entry in data.get("fixtures", []):  # read and check every file first...
        name = str(entry["file"])
        file = path.parent / name
        if not _is_file_name(name) or not file.is_file():
            raise ConfigError(
                f"{entry['id']}: {name} is not a file in the manifest's folder; nothing was "
                "refreshed"
            )
        content = file.read_bytes()
        facts = _header_facts(content, file)  # a file a replay could not load is never pinned
        fresh.append(
            {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), **facts}
        )
    changes: list[str] = []
    for entry, values in zip(data.get("fixtures", []), fresh, strict=True):  # ...then change
        for field, value in values.items():
            if entry.get(field) != value:
                changes.append(f"{entry['id']}: {field} {entry.get(field)!r} -> {value!r}")
                entry[field] = value
    if changes:
        path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return changes


# --- helpers ------------------------------------------------------------------------------


def _manifest(manifest: Path | None) -> Path:
    # MANIFEST is looked up when called, not when this module was imported, so a test can
    # point the whole runtime at another manifest (monkeypatch recordings.MANIFEST).
    return manifest if manifest is not None else MANIFEST


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path.name} is not valid JSON ({exc})") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ConfigError(f"{path.name}: not a {SCHEMA} file")
    if not isinstance(data.get("fixtures", []), list):
        raise ConfigError(f"{path.name}: 'fixtures' must be a list")
    return data


def _is_file_name(name: str) -> bool:
    """A plain file name in the manifest's folder: no folder part, no '..', no hidden file."""
    return bool(name) and Path(name).name == name and not name.startswith(".")


def _header_facts(content: bytes, file: Path) -> dict[str, str]:
    """The facts an entry repeats from its recording's header (HEADER_FACTS), after parsing
    the WHOLE recording as a replay would: a file a replay could not load is never pinned."""
    header = Recording.parse(content, file).header  # ConfigError when it is not a recording
    facts: dict[str, str] = {}
    for field, key in HEADER_FACTS.items():
        value = header.get(key)
        if not isinstance(value, str):
            raise ConfigError(f"{file.name}: the recording's header has no {key}")
        facts[field] = value
    return facts


def _entry_problems(info: FixtureInfo, folder: Path) -> list[str]:
    """What is wrong with one entry (see check_manifest)."""
    where = f"{info.id} ({info.file})"
    if not _is_file_name(info.file) or not info.file.endswith(".jsonl"):
        return [f"{where}: 'file' must be a .jsonl file name in the manifest's folder"]
    file = folder / info.file
    if not file.is_file():
        return [f"{where}: the file is missing"]
    problems: list[str] = []
    if not _SHA256.fullmatch(info.sha256):
        problems.append(f"{where}: 'sha256' is not a lowercase SHA-256 hex digest")
    content = file.read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if actual != info.sha256:
        problems.append(
            f"{where}: altered: SHA-256 {actual[:12]}..., the manifest pins "
            f"{info.sha256[:12]}... (re-record it, or --update after a deliberate re-recording)"
        )
    if len(content) != info.bytes:
        problems.append(f"{where}: {len(content)} bytes, the manifest says {info.bytes}")
    try:
        facts = _header_facts(content, file)
    except ConfigError as exc:
        return [*problems, f"{where}: {exc}"]
    for field, value in facts.items():
        if getattr(info, field) != value:
            problems.append(
                f"{where}: '{field}' is {getattr(info, field)!r}, the recording's header "
                f"says {value!r}"
            )
    if info.exported_run is not None and not _exported(info.exported_run):
        problems.append(f"{where}: exported_run {info.exported_run!r} has no README.md")
    return problems


def _exported(folder: str) -> bool:
    """A published export: a folder inside the repository with its README."""
    path = (REPO_ROOT / folder).resolve()
    return path.is_relative_to(REPO_ROOT) and (path / "README.md").is_file()
