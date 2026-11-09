"""The published real-cluster runs (evals/published.py, evals/live/MANIFEST.json).

The committed exports are M5's real runs: three on the Kind cluster and the SREGym run. Every
check is exercised on a temporary copy of them (tmp_path), so a test can alter, remove or
replace a file and see the run refused whole, with a reason that never repeats a secret. The
committed files themselves are only read.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

from evals.published import (
    BATCHES_DIR,
    LIVE_DIR,
    MANIFEST,
    MAX_FILE_BYTES,
    check_published,
    load_published,
    refresh_pins,
)
from evals.records import find_batches

BATCHES = find_batches([BATCHES_DIR])
KIND = ("run-047aac6eb0", "run-f5b9e5bf45", "run-34a3580d2f")
SREGYM = "run-9d7f47104d"
W5 = ("run-7bc5159b6d", "run-db560f6568", "run-62f9029f00")  # M6 W5: the held-out SREGym runs
SREGYM_FOLDER = "2026-11-07-qwen3-8b-sregym-human-approved-prompts-v4.7"


def copy_published(tmp_path: Path) -> Path:
    """A copy of evals/live/ (the manifest and its folders); returns the copy's manifest."""
    live = tmp_path / "live"
    live.mkdir()
    shutil.copy(MANIFEST, live / "MANIFEST.json")
    for entry in json.loads(MANIFEST.read_text())["runs"]:
        shutil.copytree(LIVE_DIR / entry["folder"], live / entry["folder"])
    return live / "MANIFEST.json"


def load(manifest: Path, **kwargs: Any) -> Any:
    kwargs.setdefault("batches", BATCHES)
    return load_published(manifest, **kwargs)


def edit_manifest(manifest: Path, change: Any) -> None:
    data = json.loads(manifest.read_text())
    change(data)
    manifest.write_text(json.dumps(data, indent=2))


def edit_lines(path: Path, change: Any) -> None:
    """Rewrite a JSON-lines file, one parsed line at a time (`change` returns the new line)."""
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    kept = [change(i, line) for i, line in enumerate(lines)]
    path.write_text("".join(json.dumps(line) + "\n" for line in kept if line is not None))


def edit_json(path: Path, change: Any) -> None:
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def rejected_reason(index: Any, run_id: str) -> str:
    assert index.get(run_id) is None, f"{run_id} must not be shown"
    reasons = [r.reason for r in index.rejected if r.run_id == run_id]
    assert len(reasons) == 1, index.rejected
    return reasons[0]


# --- the committed runs ----------------------------------------------------------------------


def test_the_committed_published_runs_all_load_with_their_provenance() -> None:
    index = load_published()
    assert index.manifest_error is None
    assert index.rejected == ()
    assert [run.run_id for run in index.runs] == [*KIND, SREGYM, *W5]
    assert check_published() == []
    for run in index.runs:
        assert run.record.run_id == run.run_id
        assert run.record.environment is not None
        assert run.record.environment.is_simulated is False
        assert run.record.environment.kind == run.entry.target
        assert run.events[0].payload["to"] == "CREATED"
        assert run.events[-1].category == "run.summary"
        assert run.record.provider_info is not None
        assert run.record.provider_info.origin == "local_llm"
        assert run.record.provider_info.model == "qwen3:8b"
        if run.entry.run_id in W5:  # the held-out runs: no fix proposed, nothing verified
            assert run.eval_record.final_state == "FAILED" and not run.eval_record.oracle_verified
        else:
            assert run.eval_record.final_state == "RESOLVED"
            assert run.eval_record.oracle_verified is True
        assert run.batch.mode == "real_kubernetes"
    assert {index.get(r).entry.target for r in KIND} == {"kind"}  # type: ignore[union-attr]


def test_the_sregym_grader_is_kept_apart_from_recaps_oracle() -> None:
    sregym = load_published().get(SREGYM)
    assert sregym is not None
    assert sregym.entry.target == "sregym"
    assert sregym.eval_record.benchmark_verdict == "pass"  # SREGym's run-oracle.py, by hand
    assert sregym.batch.benchmark is not None and "run-oracle.py" in sregym.batch.benchmark
    assert sregym.record.recovery is not None and sregym.record.recovery.verified  # RECAP's
    assert any("Not a held-out benchmark result" in note for note in sregym.entry.notes)
    for run_id in KIND:  # the Kind runs had no benchmark grader: no verdict, never "fail"
        kind = load_published().get(run_id)
        assert kind is not None and kind.eval_record.benchmark_verdict is None


def test_failed_and_blocked_runs_are_records_without_a_trace_never_successes() -> None:
    records = load_published().records_without_trace
    assert len(records) == 8
    assert {r.record.target for r in records} == {"kind", "sregym"}
    for item in records:
        assert item.record.final_state in {"FAILED", "BLOCKED"}
        assert item.record.success is False and item.record.oracle_verified is False
        assert item.record.failure  # one named category each
        assert item.record.run_id not in {*KIND, SREGYM}
    graded = {r.record.run_id: r.record.benchmark_verdict for r in records}
    assert graded["run-e08c17d46e"] == "fail"  # the grader's verdict on a blocked run
    starts = [r.record.started_at for r in records]
    assert starts == sorted(starts)


# --- altered, replaced, missing --------------------------------------------------------------


def test_an_altered_file_is_refused_whole_and_the_others_still_load(tmp_path: Path) -> None:
    manifest = copy_published(tmp_path)
    trace = manifest.parent / SREGYM_FOLDER / "trace.jsonl"
    trace.write_bytes(trace.read_bytes().replace(b"geo", b"gEo", 1))
    index = load(manifest)
    assert "does not match the SHA-256 and size the manifest pins" in rejected_reason(index, SREGYM)
    assert [run.run_id for run in index.runs] == [*KIND, *W5]


def test_a_missing_file_or_an_unpinned_required_file_is_refused(tmp_path: Path) -> None:
    manifest = copy_published(tmp_path)
    (manifest.parent / SREGYM_FOLDER / "recording.jsonl").unlink()
    assert rejected_reason(load(manifest), SREGYM) == "recording.jsonl is missing"

    def drop_trace_pin(data: dict[str, Any]) -> None:
        del data["runs"][3]["files"]["trace.jsonl"]

    edit_manifest(manifest, drop_trace_pin)
    index = load(manifest)
    assert index.runs == ()
    assert "pins no trace.jsonl" in (index.manifest_error or "")


def test_a_run_without_its_evaluation_record_is_refused(tmp_path: Path) -> None:
    index = load(copy_published(tmp_path), batches=[])
    assert index.runs == ()
    assert "no real-cluster evaluation batch" in rejected_reason(index, SREGYM)


# --- versions and schemas ----------------------------------------------------------------------


def test_another_manifest_schema_version_is_refused_and_nothing_is_shown(tmp_path: Path) -> None:
    manifest = copy_published(tmp_path)
    edit_manifest(manifest, lambda data: data.update(schema_version="recap-published-runs-v2"))
    index = load(manifest)
    assert index.runs == ()
    problem = index.manifest_error or ""
    assert "'recap-published-runs-v2' is not 'recap-published-runs-v1'" in problem


def test_an_event_schema_this_recap_cannot_read_is_refused(tmp_path: Path) -> None:
    manifest = copy_published(tmp_path)
    trace = manifest.parent / SREGYM_FOLDER / "trace.jsonl"
    edit_lines(trace, lambda i, e: {**e, "schema_version": "recap.v9"} if i == 3 else e)
    refresh_pins(manifest)  # re-pinned: the schema check, not the hash, must refuse it
    assert "event schema 'recap.v9' is not one this RECAP reads" in rejected_reason(
        load(manifest), SREGYM
    )


def test_a_record_that_does_not_match_recaps_run_record_is_refused(tmp_path: Path) -> None:
    manifest = copy_published(tmp_path)
    edit_json(manifest.parent / SREGYM_FOLDER / "run.json", lambda r: r.update(surprise=1))
    refresh_pins(manifest)
    assert "run.json does not match this RECAP's run record" in rejected_reason(
        load(manifest), SREGYM
    )


# --- the files must tell one story ------------------------------------------------------------


def _other_run(i: int, event: dict[str, Any]) -> dict[str, Any]:
    return {**event, "run_id": "run-0000000000"} if i == 5 else event


def _simulated(record: dict[str, Any]) -> None:
    record["environment"]["is_simulated"] = True


def _replay(record: dict[str, Any]) -> None:
    record["provider_info"]["origin"] = "replay"


def _other_recording(i: int, line: dict[str, Any]) -> dict[str, Any]:
    return {**line, "recording_id": "20991231-000000-elsewhere"} if i == 0 else line


def _no_decision(i: int, event: dict[str, Any]) -> dict[str, Any] | None:
    return None if event["category"] == "approval.decided" else event


def _other_end(i: int, event: dict[str, Any]) -> dict[str, Any]:
    if event["category"] == "run.summary":
        return {**event, "payload": {**event["payload"], "state": "FAILED"}}
    return event


@pytest.mark.parametrize(
    ("file", "change", "reason"),
    [
        ("trace.jsonl", _other_run, "holds events of another run"),
        ("run.json", _simulated, "the run recorded the simulator, not the declared target sregym"),
        ("run.json", _replay, "a replay never runs against a real cluster"),
        ("recording.jsonl", _other_recording, "not this run's own recording"),
        ("trace.jsonl", _no_decision, "is in run.json but not in the trace"),
        ("trace.jsonl", _other_end, "does not end with the run's final state"),
    ],
)
def test_files_that_disagree_with_each_other_are_refused(
    tmp_path: Path, file: str, change: Any, reason: str
) -> None:
    manifest = copy_published(tmp_path)
    path = manifest.parent / SREGYM_FOLDER / file
    if file == "run.json":
        edit_json(path, change)
    else:
        edit_lines(path, change)
    refresh_pins(manifest)
    assert reason in rejected_reason(load(manifest), SREGYM)


def test_a_declared_target_the_run_did_not_work_on_is_refused(tmp_path: Path) -> None:
    manifest = copy_published(tmp_path)
    edit_manifest(manifest, lambda data: data["runs"][0].update(target="sregym"))
    assert "not the declared target sregym" in rejected_reason(load(manifest), KIND[0])


# --- unsafe artifacts -------------------------------------------------------------------------


@pytest.mark.parametrize("folder", ["../outside", "/etc", "2026-11-07-a/../../b", "x"])
def test_a_folder_is_a_name_inside_evals_live_never_a_path(tmp_path: Path, folder: str) -> None:
    manifest = copy_published(tmp_path)
    edit_manifest(manifest, lambda data: data["runs"][0].update(folder=folder))
    index = load(manifest)
    assert index.runs == ()
    assert "folder" in (index.manifest_error or "")


def test_links_are_never_followed(tmp_path: Path) -> None:
    manifest = copy_published(tmp_path)
    live = manifest.parent
    real = live / SREGYM_FOLDER
    moved = tmp_path / "elsewhere"
    real.rename(moved)
    real.symlink_to(moved, target_is_directory=True)
    assert "symbolic link" in rejected_reason(load(manifest), SREGYM)

    real.unlink()
    moved.rename(real)
    readme = real / "README.md"
    outside = tmp_path / "README.md"
    readme.rename(outside)
    readme.symlink_to(outside)
    assert rejected_reason(load(manifest), SREGYM) == (
        "README.md is a symbolic link: only real files are read"
    )


def test_text_shaped_like_a_credential_refuses_the_run_without_repeating_it(tmp_path: Path) -> None:
    manifest = copy_published(tmp_path)
    fake = "gsk" + "_" + "Q" * 24  # shaped like a Groq key; built here so no file holds one
    edit_lines(
        manifest.parent / SREGYM_FOLDER / "trace.jsonl",
        lambda i, e: {**e, "payload": {**e["payload"], "note": f"key {fake}"}} if i == 7 else e,
    )
    refresh_pins(manifest)
    reason = rejected_reason(load(manifest), SREGYM)
    assert "holds text shaped like a credential" in reason
    assert fake not in reason and "QQQQ" not in reason
    assert hashlib.sha256(fake.encode()).hexdigest()[:12] in reason  # identifies, reveals nothing


def test_malformed_json_and_deep_nesting_are_refused(tmp_path: Path) -> None:
    manifest = copy_published(tmp_path)
    (manifest.parent / SREGYM_FOLDER / "run.json").write_text("[" * 200_000)
    refresh_pins(manifest)
    assert rejected_reason(load(manifest), SREGYM) == "run.json is not valid JSON"

    trace = manifest.parent / kind_folder(0) / "trace.jsonl"
    trace.write_text(trace.read_text() + "{not json\n")
    refresh_pins(manifest)
    assert "is not valid JSON" in rejected_reason(load(manifest), KIND[0])


def test_an_oversized_file_is_refused_before_it_is_read(tmp_path: Path) -> None:
    manifest = copy_published(tmp_path)
    readme = manifest.parent / SREGYM_FOLDER / "README.md"
    readme.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    assert rejected_reason(load(manifest), SREGYM) == (
        f"README.md is larger than {MAX_FILE_BYTES} bytes"
    )


# --- read-only, offline ---------------------------------------------------------------------------


def test_loading_reads_only_and_opens_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = copy_published(tmp_path)
    before = {p: p.read_bytes() for p in sorted(manifest.parent.rglob("*")) if p.is_file()}

    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the published runs must load offline")

    monkeypatch.setattr(socket, "socket", no_network)
    index = load(manifest)
    assert len(index.runs) == len(KIND) + 1 + len(W5)
    after = {p: p.read_bytes() for p in sorted(manifest.parent.rglob("*")) if p.is_file()}
    assert after == before


def test_refresh_pins_repins_a_deliberately_changed_file_only(tmp_path: Path) -> None:
    manifest = copy_published(tmp_path)
    readme = manifest.parent / SREGYM_FOLDER / "README.md"
    readme.write_text(readme.read_text() + "\nA correction made in review.\n")
    assert len(load(manifest).runs) == len(KIND) + len(W5)
    assert refresh_pins(manifest) == [f"{SREGYM_FOLDER}/README.md"]
    assert len(load(manifest).runs) == len(KIND) + 1 + len(W5)
    assert refresh_pins(manifest) == []


def kind_folder(i: int) -> str:
    return str(json.loads(MANIFEST.read_text())["runs"][i]["folder"])
