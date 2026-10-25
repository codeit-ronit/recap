"""Committed recordings are content-addressed (recap/providers/recordings.py), replayed by
manifest id (the replay provider in recap/providers/factory.py), and checked or re-pinned by
scripts/recordings_manifest.py.

Offline: the committed files are only read; every change happens to a copy in a temporary
folder.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import REPO_ROOT, Settings
from recap.core.errors import ConfigError
from recap.providers import recordings
from recap.providers.factory import REPLAY_ENV, create_provider
from recap.providers.protocol import PROMPT_VERSION
from recap.providers.transport import ReplayTransport
from scripts import recordings_manifest

GOLDEN = "qwen3-8b-unfamiliar-parallel"
SINGLE = "qwen3-8b-bad-deployment"


def entry(fixture_id: str) -> recordings.FixtureInfo:
    [info] = [f for f in recordings.list_fixtures() if f.id == fixture_id]
    return info


def copy_of_folder(folder: Path, *ids: str) -> Path:
    """A copy of the manifest (only these entries) and their recordings in `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    entries = [entry(i).model_dump(mode="json") for i in ids]
    for item in entries:
        (folder / item["file"]).write_bytes((recordings.RECORDINGS_DIR / item["file"]).read_bytes())
    return write_manifest(folder, entries)


def write_manifest(folder: Path, entries: list[dict[str, Any]], schema: str = "") -> Path:
    manifest = folder / "MANIFEST.json"
    data = {"schema": schema or recordings.SCHEMA, "fixtures": entries}
    manifest.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return manifest


def change_one_byte(path: Path, old: bytes = b'"eval_count": 272', new: bytes = b"3") -> None:
    """Change the last digit after `old` (a recorded token count) to `new`."""
    data = bytearray(path.read_bytes())
    at = data.index(old) + len(old) - 1
    data[at : at + 1] = new
    path.write_bytes(bytes(data))


# --- the committed manifest ------------------------------------------------------------------


def test_the_committed_manifest_matches_every_recording() -> None:
    """What CI checks on every push: each pin, size and header fact, and no unlisted file."""
    assert recordings.check_manifest() == []
    assert [f.id for f in recordings.list_fixtures()] == [GOLDEN, SINGLE]
    listed = {f.file for f in recordings.list_fixtures()}
    assert listed == {p.name for p in recordings.RECORDINGS_DIR.glob("*.jsonl")}


def test_the_golden_fixture_is_the_published_owner_approved_run() -> None:
    golden = entry(GOLDEN)
    assert (golden.scenario, golden.strategy, golden.model) == (
        "m3-inventory-leak-behind-frontend",
        "parallel",
        "qwen3:8b",
    )
    assert golden.prompt_version == PROMPT_VERSION  # it still replays with today's prompts
    assert {"golden", "multi-agent", "human-approved"} <= set(golden.tags)
    # The published export holds the very same bytes (content addressing works across copies).
    assert golden.exported_run is not None
    exported = REPO_ROOT / golden.exported_run / "recording.jsonl"
    assert hashlib.sha256(exported.read_bytes()).hexdigest() == golden.sha256
    assert recordings.fixture_for(golden.sha256) == golden


# --- reading a fixture ------------------------------------------------------------------------


def test_read_fixture_returns_the_very_bytes_it_checked() -> None:
    golden = entry(GOLDEN)
    verified = recordings.read_fixture(GOLDEN)
    assert verified.info == golden and verified.path.name == golden.file
    assert hashlib.sha256(verified.data).hexdigest() == golden.sha256
    assert len(verified.data) == golden.bytes
    assert recordings.fixture_path(GOLDEN) == verified.path


def test_an_unknown_id_is_refused() -> None:
    with pytest.raises(ConfigError, match="unknown recording 'no-such-run'"):
        recordings.read_fixture("no-such-run")


def test_one_changed_byte_is_refused_without_naming_a_local_folder(tmp_path: Path) -> None:
    manifest = copy_of_folder(tmp_path, GOLDEN)
    assert recordings.read_fixture(GOLDEN, manifest).data  # the exact copy is the fixture
    change_one_byte(tmp_path / entry(GOLDEN).file)

    with pytest.raises(ConfigError, match=f"recording '{GOLDEN}' was altered") as refused:
        recordings.read_fixture(GOLDEN, manifest)
    assert str(tmp_path) not in str(refused.value)
    assert recordings.fixture_for(recordings.sha256_of(tmp_path / entry(GOLDEN).file)) is None


@pytest.mark.parametrize("name", ["../ollama-qwen3-8b-unfamiliar-parallel.jsonl", ".hidden.jsonl"])
def test_a_file_outside_the_manifests_folder_is_refused(tmp_path: Path, name: str) -> None:
    inside = tmp_path / "inside"
    manifest = copy_of_folder(inside, GOLDEN)
    (tmp_path / entry(GOLDEN).file).write_bytes((inside / entry(GOLDEN).file).read_bytes())
    item = entry(GOLDEN).model_dump(mode="json") | {"file": name}
    write_manifest(inside, [item])
    with pytest.raises(ConfigError, match="is not a file name"):
        recordings.read_fixture(GOLDEN, manifest)
    assert any("'file' must be" in p for p in recordings.check_manifest(manifest))


@pytest.mark.parametrize(
    ("mangle", "message"),
    [
        (lambda items: items + items, "two entries have the id"),
        (lambda items: [{k: v for k, v in items[0].items() if k != "sha256"}], "check sha256"),
        (lambda items: [items[0] | {"surprise": 1}], "check surprise"),
    ],
    ids=["duplicate-id", "missing-field", "unknown-field"],
)
def test_a_broken_manifest_is_refused_as_a_whole(tmp_path: Path, mangle: Any, message: str) -> None:
    copy_of_folder(tmp_path, GOLDEN)
    manifest = write_manifest(tmp_path, mangle([entry(GOLDEN).model_dump(mode="json")]))
    with pytest.raises(ConfigError, match=message):
        recordings.list_fixtures(manifest)
    assert recordings.check_manifest(manifest) != []


def test_a_manifest_of_another_kind_is_refused(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path, [], schema="something-else")
    with pytest.raises(ConfigError, match="not a recap-recordings-manifest-v1 file"):
        recordings.list_fixtures(manifest)
    manifest.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        recordings.list_fixtures(manifest)
    assert recordings.list_fixtures(tmp_path / "absent.json") == []


# --- check_manifest ---------------------------------------------------------------------------


def _alter(folder: Path) -> None:
    change_one_byte(folder / entry(GOLDEN).file)


def _wrong_size(folder: Path) -> None:
    write_manifest(folder, [entry(GOLDEN).model_dump(mode="json") | {"bytes": 1}])


def _wrong_fact(folder: Path) -> None:
    item = entry(GOLDEN).model_dump(mode="json") | {"recording_id": "20990101-000000-ollama-x"}
    write_manifest(folder, [item])


def _missing(folder: Path) -> None:
    (folder / entry(GOLDEN).file).unlink()


def _unlisted(folder: Path) -> None:
    (folder / "extra.jsonl").write_bytes((folder / entry(GOLDEN).file).read_bytes())


def _no_export(folder: Path) -> None:
    item = entry(GOLDEN).model_dump(mode="json") | {"exported_run": "evals/live/no-such-run/"}
    write_manifest(folder, [item])


def _not_a_recording(folder: Path) -> None:
    path = folder / entry(GOLDEN).file
    path.write_text('{"type": "exchange"}\n', encoding="utf-8")


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        (_alter, "altered: SHA-256"),
        (_wrong_size, "bytes, the manifest says 1"),
        (_wrong_fact, "'recording_id' is '20990101-000000-ollama-x'"),
        (_missing, "the file is missing"),
        (_unlisted, "extra.jsonl is not in MANIFEST.json"),
        (_no_export, "has no README.md"),
        (_not_a_recording, "is not a RECAP recording"),
    ],
    ids=["altered", "size", "header-fact", "missing", "unlisted", "export", "not-a-recording"],
)
def test_check_names_each_kind_of_problem(tmp_path: Path, damage: Any, expected: str) -> None:
    manifest = copy_of_folder(tmp_path, GOLDEN)
    assert recordings.check_manifest(manifest) == []
    damage(tmp_path)
    problems = recordings.check_manifest(manifest)
    assert any(expected in problem for problem in problems), problems
    assert all(str(tmp_path) not in problem for problem in problems)  # no local folder


# --- refresh_manifest (--update) --------------------------------------------------------------


def test_update_re_pins_a_deliberate_re_recording_and_keeps_the_prose(tmp_path: Path) -> None:
    manifest = copy_of_folder(tmp_path, GOLDEN, SINGLE)
    path = tmp_path / entry(GOLDEN).file
    # A "re-recording": another recording ID in the header, and other bytes.
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0]) | {"recording_id": "20991231-235959-ollama-abcdef"}
    lines[0] = json.dumps(header, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert len(recordings.check_manifest(manifest)) == 2  # the hash and the recording ID

    changes = recordings.refresh_manifest(manifest)

    assert [c.split(":")[0] for c in changes] == [GOLDEN, GOLDEN]  # the other entry is untouched
    assert any("sha256" in c for c in changes) and any("recording_id" in c for c in changes)
    assert recordings.check_manifest(manifest) == []
    refreshed = recordings.list_fixtures(manifest)[0]
    assert refreshed.sha256 == recordings.sha256_of(path)
    assert refreshed.recording_id == "20991231-235959-ollama-abcdef"
    before = entry(GOLDEN)
    for field in ("title", "description", "agents", "approved_by", "tags", "exported_run"):
        assert getattr(refreshed, field) == getattr(before, field)  # prose is the owner's
    text = manifest.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=1) + "\n"  # the committed file's format


def test_update_writes_nothing_when_every_pin_matches(tmp_path: Path) -> None:
    manifest = copy_of_folder(tmp_path, GOLDEN, SINGLE)
    before = manifest.read_bytes()
    assert recordings.refresh_manifest(manifest) == []
    assert manifest.read_bytes() == before
    # The committed manifest is written exactly like this: an update would leave it as it is.
    committed = recordings.MANIFEST.read_text(encoding="utf-8")
    assert committed == json.dumps(json.loads(committed), indent=1) + "\n"


@pytest.mark.parametrize("damage", [_missing, _not_a_recording], ids=["missing", "not-a-recording"])
def test_update_refuses_a_file_it_could_not_replay_and_writes_nothing(
    tmp_path: Path, damage: Any
) -> None:
    manifest = copy_of_folder(tmp_path, GOLDEN)
    before = manifest.read_bytes()
    damage(tmp_path)
    with pytest.raises(ConfigError):
        recordings.refresh_manifest(manifest)
    assert manifest.read_bytes() == before


# --- scripts/recordings_manifest.py -----------------------------------------------------------


def test_the_script_checks_the_committed_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    assert recordings_manifest.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "fixtures/recordings/MANIFEST.json: 2 recording(s), every one matches" in out


def test_the_script_fails_an_altered_copy_and_update_repairs_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = copy_of_folder(tmp_path, GOLDEN)
    _alter(tmp_path)

    assert recordings_manifest.main(["--check", "--manifest", str(manifest)]) == 1
    out = capsys.readouterr().out
    assert "problem:" in out and "altered" in out

    assert recordings_manifest.main(["--update", "--manifest", str(manifest)]) == 0
    out = capsys.readouterr().out
    assert f"updated {GOLDEN}: sha256" in out and "Review each changed entry" in out
    assert recordings_manifest.main(["--check", "--manifest", str(manifest)]) == 0


def test_the_script_update_reports_what_it_cannot_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = copy_of_folder(tmp_path, GOLDEN)
    _unlisted(tmp_path)  # a new recording needs an entry written by a person
    assert recordings_manifest.main(["--update", "--manifest", str(manifest)]) == 1
    assert "extra.jsonl is not in MANIFEST.json" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["--check", "--update"]])
def test_the_script_needs_exactly_one_mode(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        recordings_manifest.main(argv)
    assert exited.value.code == 2


# --- replay by manifest id (the replay provider) ----------------------------------------------


def test_the_replay_provider_loads_a_fixture_by_its_manifest_id(settings: Settings) -> None:
    golden = entry(GOLDEN)
    provider = create_provider("replay", settings, replay_fixture=GOLDEN)
    info = provider.info
    assert (info.kind, info.origin, info.name) == ("replay", "replay", "replay:ollama:qwen3:8b")
    assert info.recording == f"fixtures/recordings/{golden.file}"  # repo-relative
    assert info.replay_of is not None
    assert info.replay_of["fixture"] == GOLDEN
    assert info.replay_of["sha256"] == golden.sha256
    assert info.replay_of["recording_id"] == golden.recording_id
    assert info.replay_of["original_origin"] == "local_llm"
    assert (info.replay_of["model"], info.replay_of["prompt_version"]) == (
        golden.model,
        golden.prompt_version,
    )


def test_the_manifest_id_wins_over_the_replay_environment_variable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that names a fixture gets that fixture, whatever the server's shell says."""
    monkeypatch.setenv(REPLAY_ENV, str(recordings.RECORDINGS_DIR / entry(SINGLE).file))
    chosen = create_provider("replay", settings, replay_fixture=GOLDEN)
    assert chosen.info.replay_of is not None and chosen.info.replay_of["fixture"] == GOLDEN
    ambient = create_provider("replay", settings)  # no fixture: the variable, as before
    assert ambient.info.replay_of is not None and ambient.info.replay_of["fixture"] == SINGLE


@pytest.mark.parametrize("provider", ["deterministic", "ollama"])
def test_only_the_replay_provider_takes_a_fixture(settings: Settings, provider: str) -> None:
    with pytest.raises(ConfigError, match=f"needs the replay provider, not '{provider}'"):
        create_provider(provider, settings, replay_fixture=GOLDEN)


def test_an_altered_fixture_is_refused_before_a_replay_transport_exists(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = copy_of_folder(tmp_path, GOLDEN)
    _alter(tmp_path)
    monkeypatch.setattr(recordings, "MANIFEST", manifest)
    built: list[object] = []
    monkeypatch.setattr(ReplayTransport, "__init__", lambda self, rec: built.append(rec))
    with pytest.raises(ConfigError, match="was altered"):
        create_provider("replay", settings, replay_fixture=GOLDEN)
    with pytest.raises(ConfigError, match="unknown recording"):
        create_provider("replay", settings, replay_fixture=SINGLE)  # not in that manifest
    assert built == []


def test_a_recording_given_by_path_is_named_by_its_bytes(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RECAP_REPLAY_RECORDING keeps working for scripts. A copy of a committed fixture
    anywhere is that fixture (same bytes), shown by its file name only when it is outside the
    repository; a copy with one changed byte is a replay, but of no committed recording."""
    golden = entry(GOLDEN)
    copy = tmp_path / golden.file
    copy.write_bytes((recordings.RECORDINGS_DIR / golden.file).read_bytes())
    monkeypatch.setenv(REPLAY_ENV, str(copy))
    info = create_provider("replay", settings).info
    assert info.replay_of is not None and info.replay_of["fixture"] == GOLDEN
    assert info.recording == golden.file  # no local folder in a run record

    change_one_byte(copy)
    info = create_provider("replay", settings).info
    assert info.replay_of is not None and info.replay_of["fixture"] is None
    assert info.replay_of["sha256"] == recordings.sha256_of(copy) != golden.sha256

    # The committed file by its absolute path: shown repo-relative.
    monkeypatch.setenv(REPLAY_ENV, str(recordings.RECORDINGS_DIR / golden.file))
    assert (
        create_provider("replay", settings).info.recording == f"fixtures/recordings/{golden.file}"
    )


def test_a_missing_recording_is_reported_without_its_local_folder(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REPLAY_ENV, str(tmp_path / "gone.jsonl"))
    with pytest.raises(ConfigError, match="recording not found: gone.jsonl") as refused:
        create_provider("replay", settings)
    assert str(tmp_path) not in str(refused.value)


def test_the_replay_answers_from_the_very_bytes_that_were_checked(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing can change between the check and the replay: when the file changes right
    after its check (another process writing it), the replay still answers from the bytes
    whose SHA-256 matched the pin, never from a second read of the file."""
    monkeypatch.setattr(recordings, "MANIFEST", copy_of_folder(tmp_path, GOLDEN))
    check = recordings.read_fixture

    def checked_then_altered(fixture_id: str, manifest: Path | None = None) -> Any:
        verified = check(fixture_id, manifest)
        change_one_byte(verified.path)  # the proposal answer's 272 output tokens become 273
        return verified

    monkeypatch.setattr(recordings, "read_fixture", checked_then_altered)
    provider = create_provider("replay", settings, replay_fixture=GOLDEN)
    answered = provider.transport.recording.exchanges[-1]["response"]["body"]  # type: ignore[attr-defined]
    assert answered["eval_count"] == 272
    assert b'"eval_count": 273' in (tmp_path / entry(GOLDEN).file).read_bytes()
    assert provider.info.replay_of is not None
    assert provider.info.replay_of["sha256"] == entry(GOLDEN).sha256
