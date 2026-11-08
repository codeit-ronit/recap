"""Published real-cluster runs (evals/live/): an allowlist, checked byte for byte, read-only.

    evals/live/MANIFEST.json  --> load_published()  --> the API's run views: GET /published,
                                                        GET /runs/{id}, its trace, evidence...
    scripts/published_runs.py --> check_published() and refresh_pins()

M5's real Kubernetes runs (Kind and SREGym) were exported with scripts/export_run_trace.py:
each folder holds the run's record (run.json), its trace (trace.jsonl), the model's recording
(recording.jsonl) and a README a person wrote. This module makes them readable on a fresh
checkout, with no cluster, no model and no cloud account, and never more than readable:

  - Only the folders the manifest names are read, and in them only the four known file names.
    A folder is a plain name inside evals/live/, never a path, a link or anything outside it.
  - The manifest pins every file's SHA-256 and size. Each file's bytes are read once, hashed,
    and those same bytes are parsed, as JSON only: nothing executable is ever deserialized.
  - The record and every trace event are validated by RECAP's own models (RunRecord,
    TraceEvent): an incompatible schema is refused, never guessed.
  - Across the files: one run id everywhere; the target the manifest declares is the one the
    run recorded, a real cluster and not the simulator; the recording is the run's own; every
    approval in the record is in the trace; the trace ends in the record's final state; and
    the run's evaluation record (evals/reports/batches/) agrees with it.
  - No text shaped like a credential (recap/telemetry/redact.py's patterns) may appear in any
    file. Such a run is refused whole, and the reason never repeats the value.

A run that fails a check is not shown at all: the index lists it as rejected, with the reason.
What loads is evidence only. It is never written to this server's stores, never followed as a
live run and never replayed (only fixtures/recordings/ can be). Its approvals are records of a
past decision, never permissions: the approval store does not know them.

The manifest's prose (title, notes) is written by a person and checked in review; every fact a
screen shows comes from the files themselves.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from evals.records import EvalBatch, EvalRecord, find_batches
from recap.core.config import REPO_ROOT
from recap.core.models import SCHEMA_VERSION, Contract, RunRecord, TraceEvent
from recap.telemetry.redact import SECRET_PATTERNS

SCHEMA = "recap-published-runs-v1"
LIVE_DIR = REPO_ROOT / "evals" / "live"
MANIFEST = LIVE_DIR / "MANIFEST.json"
BATCHES_DIR = REPO_ROOT / "evals" / "reports" / "batches"

FileName = Literal["run.json", "trace.jsonl", "recording.jsonl", "README.md"]
REQUIRED_FILES: tuple[FileName, ...] = ("run.json", "trace.jsonl", "recording.jsonl")
MAX_FILE_BYTES = 2_000_000  # the largest M5 export file is 126 KB
MAX_MANIFEST_BYTES = 256_000
MAX_EVENTS = 5_000
TRACE_SCHEMAS = frozenset({SCHEMA_VERSION})  # the TraceEvent.schema_version values read here
RECORDING_SCHEMA = "recap-recording-v1"

_FOLDER = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9][a-z0-9.-]{0,100}$"
_BATCH = r"^[0-9A-Za-z][0-9A-Za-z._-]{0,120}$"


class PublishedRefused(Exception):
    """One published run failed a check: the reason, safe to show (it never quotes a secret)."""


# ======================================================================================
# The manifest (the allowlist)
# ======================================================================================


class PinnedFile(Contract):
    """One file of a published run: its exact size and SHA-256."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0, le=MAX_FILE_BYTES)


class PublishedEntry(Contract):
    """One published run: where it is, what it ran against, and its pinned files."""

    run_id: str = Field(pattern=r"^run-[0-9a-f]{10}$")
    folder: str = Field(pattern=_FOLDER)  # a folder name inside evals/live/, never a path
    target: Literal["kind", "sregym"]  # the real cluster the run worked on
    batch: str = Field(pattern=_BATCH)  # the evaluation batch that holds its record
    title: str = Field(min_length=1, max_length=160)
    notes: list[str] = Field(default_factory=list, max_length=8)  # a person's, checked in review
    files: dict[FileName, PinnedFile]

    @model_validator(mode="after")
    def _complete(self) -> PublishedEntry:
        missing = [name for name in REQUIRED_FILES if name not in self.files]
        if missing:
            raise ValueError(f"{self.folder}: the manifest pins no {', '.join(missing)}")
        if any(len(note) > 400 for note in self.notes):
            raise ValueError(f"{self.folder}: a note is longer than 400 characters")
        return self


class PublishedManifest(Contract):
    """evals/live/MANIFEST.json."""

    schema_version: Literal["recap-published-runs-v1"]
    description: str = ""
    runs: list[PublishedEntry]

    @model_validator(mode="after")
    def _unique(self) -> PublishedManifest:
        for name in ("run_id", "folder"):
            values = [getattr(entry, name) for entry in self.runs]
            repeated = sorted({v for v in values if values.count(v) > 1})
            if repeated:
                raise ValueError(f"the manifest lists {', '.join(repeated)} twice")
        return self


# ======================================================================================
# What loads
# ======================================================================================


@dataclass(frozen=True)
class PublishedRun:
    """A published run that passed every check: evidence to read, never anything to act on."""

    entry: PublishedEntry
    record: RunRecord
    events: tuple[TraceEvent, ...]
    recording_id: str
    eval_record: EvalRecord
    batch: EvalBatch  # its evaluation batch: the grader behind benchmark_verdict, limitations

    @property
    def run_id(self) -> str:
        return self.entry.run_id


@dataclass(frozen=True)
class RejectedArtifact:
    """A manifest entry that failed a check, and why (nothing of it is shown)."""

    folder: str | None
    run_id: str | None
    reason: str


@dataclass(frozen=True)
class RecordWithoutTrace:
    """A real-cluster evaluation record whose full trace was not published."""

    record: EvalRecord
    batch: EvalBatch


@dataclass(frozen=True)
class PublishedIndex:
    """Everything published, as checked: the runs that load, those refused, and the
    real-cluster evaluation records that have no published trace (failures included)."""

    runs: tuple[PublishedRun, ...] = ()
    rejected: tuple[RejectedArtifact, ...] = ()
    records_without_trace: tuple[RecordWithoutTrace, ...] = ()
    manifest_error: str | None = None
    schema_version: str = SCHEMA
    _by_id: Mapping[str, PublishedRun] = field(default_factory=dict, repr=False)

    def get(self, run_id: str) -> PublishedRun | None:
        return self._by_id.get(run_id)


def load_published(
    manifest: Path = MANIFEST,
    *,
    live_dir: Path | None = None,
    batches: Sequence[EvalBatch] | None = None,
) -> PublishedIndex:
    """Read and check every run the manifest allows. Never raises for a bad artifact: a broken
    manifest becomes `manifest_error`, a broken run a RejectedArtifact."""
    folder = (live_dir or manifest.parent).resolve()
    known = list(batches) if batches is not None else find_batches([BATCHES_DIR])
    real = [b for b in known if b.mode == "real_kubernetes"]
    try:
        parsed = _read_manifest(manifest)
    except PublishedRefused as refused:
        return PublishedIndex(
            manifest_error=str(refused),
            records_without_trace=_without_trace(real, set()),
        )
    runs: list[PublishedRun] = []
    rejected: list[RejectedArtifact] = []
    for entry in parsed.runs:
        try:
            runs.append(_load_entry(entry, folder, real))
        except PublishedRefused as refused:
            rejected.append(RejectedArtifact(entry.folder, entry.run_id, str(refused)))
    loaded = {run.run_id for run in runs}
    return PublishedIndex(
        runs=tuple(runs),
        rejected=tuple(rejected),
        records_without_trace=_without_trace(real, loaded),
        _by_id={run.run_id: run for run in runs},
    )


def check_published(manifest: Path = MANIFEST) -> list[str]:
    """What is wrong with the published runs (empty: every entry loads)."""
    index = load_published(manifest)
    if index.manifest_error:
        return [index.manifest_error]
    return [f"{r.folder}: {r.reason}" for r in index.rejected]


def refresh_pins(manifest: Path = MANIFEST) -> list[str]:
    """Re-pin each listed file's SHA-256 and size after a deliberate change to an export, and
    return what changed. Entries are never added here: a person adds them, with their prose."""
    data = json.loads(manifest.read_text(encoding="utf-8"))
    changed: list[str] = []
    for entry in data.get("runs", []):
        folder = manifest.parent / str(entry.get("folder", ""))
        for name, pin in entry.get("files", {}).items():
            path = folder / name
            content = path.read_bytes()
            new = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            if pin != new:
                changed.append(f"{entry.get('folder')}/{name}")
                entry["files"][name] = new
    if changed:
        manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


# ======================================================================================
# The checks
# ======================================================================================


def _read_manifest(path: Path) -> PublishedManifest:
    try:
        if path.is_symlink() or not path.is_file():
            raise PublishedRefused(f"{path.name}: not found")
        content = path.read_bytes()
    except OSError as exc:
        raise PublishedRefused(f"{path.name}: cannot be read ({exc.strerror})") from None
    if len(content) > MAX_MANIFEST_BYTES:
        raise PublishedRefused(f"{path.name}: larger than {MAX_MANIFEST_BYTES} bytes")
    data = _json(content, path.name)
    if isinstance(data, dict) and data.get("schema_version") != SCHEMA:
        raise PublishedRefused(
            f"{path.name}: schema {data.get('schema_version')!r} is not {SCHEMA!r}, the one this "
            "RECAP reads"
        )
    try:
        return PublishedManifest.model_validate(data)
    except ValidationError as exc:
        raise PublishedRefused(f"{path.name}: {_first_error(exc)}") from None


def _load_entry(entry: PublishedEntry, live: Path, batches: Sequence[EvalBatch]) -> PublishedRun:
    folder = _folder(entry, live)
    content = {name: _pinned_bytes(folder, name, pin) for name, pin in entry.files.items()}
    texts = {name: _text(data, name) for name, data in content.items()}
    for name, text in texts.items():
        _no_credentials(text, name)

    record = _record(content["run.json"])
    events = _events(texts["trace.jsonl"])
    header = _recording_header(texts["recording.jsonl"])
    eval_record, batch = _eval_record(entry, batches)
    _consistent(entry, record, events, header, eval_record)
    return PublishedRun(
        entry=entry,
        record=record,
        events=tuple(events),
        recording_id=str(header["recording_id"]),
        eval_record=eval_record,
        batch=batch,
    )


def _folder(entry: PublishedEntry, live: Path) -> Path:
    """The entry's folder: a real directory directly inside evals/live/, not a link."""
    path = live / entry.folder
    if path.is_symlink():
        raise PublishedRefused("the folder is a symbolic link: only real folders are read")
    if not path.is_dir():
        raise PublishedRefused(f"no folder {entry.folder} in evals/live/")
    if path.resolve().parent != live:
        raise PublishedRefused("the folder resolves outside evals/live/")
    return path


def _pinned_bytes(folder: Path, name: str, pin: PinnedFile) -> bytes:
    path = folder / name
    if path.is_symlink():
        raise PublishedRefused(f"{name} is a symbolic link: only real files are read")
    if not path.is_file():
        raise PublishedRefused(f"{name} is missing")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise PublishedRefused(f"{name} is larger than {MAX_FILE_BYTES} bytes")
    content = path.read_bytes()  # read once: these bytes are hashed, then parsed
    if len(content) != pin.bytes or hashlib.sha256(content).hexdigest() != pin.sha256:
        raise PublishedRefused(
            f"{name} does not match the SHA-256 and size the manifest pins (altered or replaced)"
        )
    return content


def _text(content: bytes, name: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise PublishedRefused(f"{name} is not UTF-8 text") from None


def _no_credentials(text: str, name: str) -> None:
    """Refuse a file holding anything shaped like a key or token, without repeating it."""
    for pattern in SECRET_PATTERNS:
        found = pattern.search(text)
        if found:
            tag = hashlib.sha256(found.group(0).encode()).hexdigest()[:12]
            raise PublishedRefused(
                f"{name} holds text shaped like a credential (sha256 {tag}…): a published run is "
                "never shown with one"
            )


def _json(content: bytes | str, name: str) -> Any:
    try:
        return json.loads(content)
    except (ValueError, RecursionError):
        raise PublishedRefused(f"{name} is not valid JSON") from None


def _record(content: bytes) -> RunRecord:
    data = _json(content, "run.json")
    if not isinstance(data, dict):
        raise PublishedRefused("run.json is not a JSON object")
    try:
        return RunRecord.model_validate(data)
    except ValidationError as exc:
        raise PublishedRefused(
            f"run.json does not match this RECAP's run record: {_first_error(exc)}"
        ) from None


def _events(text: str) -> list[TraceEvent]:
    events: list[TraceEvent] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(events) >= MAX_EVENTS:
            raise PublishedRefused(f"trace.jsonl has more than {MAX_EVENTS} events")
        data = _json(line, f"trace.jsonl line {number}")
        if not isinstance(data, dict):
            raise PublishedRefused(f"trace.jsonl line {number} is not a JSON object")
        version = data.get("schema_version")
        if version not in TRACE_SCHEMAS:
            raise PublishedRefused(
                f"trace.jsonl line {number}: event schema {version!r} is not one this RECAP reads "
                f"({', '.join(sorted(TRACE_SCHEMAS))})"
            )
        try:
            events.append(TraceEvent.model_validate(data))
        except ValidationError as exc:
            raise PublishedRefused(f"trace.jsonl line {number}: {_first_error(exc)}") from None
    if not events:
        raise PublishedRefused("trace.jsonl holds no event")
    seqs = [event.seq for event in events]
    if seqs != sorted(set(seqs)) or seqs[0] < 1:
        raise PublishedRefused("trace.jsonl: event numbers (seq) are not strictly increasing")
    return events


def _recording_header(text: str) -> dict[str, Any]:
    first = next((line for line in text.splitlines() if line.strip()), "")
    header = _json(first, "recording.jsonl") if first else None
    if not isinstance(header, dict) or header.get("type") != "header":
        raise PublishedRefused("recording.jsonl does not start with a recording header")
    if header.get("schema") != RECORDING_SCHEMA:
        raise PublishedRefused(
            f"recording.jsonl: schema {header.get('schema')!r} is not {RECORDING_SCHEMA!r}"
        )
    if not isinstance(header.get("recording_id"), str) or not header["recording_id"]:
        raise PublishedRefused("recording.jsonl: the header names no recording id")
    return header


def _eval_record(
    entry: PublishedEntry, batches: Sequence[EvalBatch]
) -> tuple[EvalRecord, EvalBatch]:
    batch = next((b for b in batches if b.batch_id == entry.batch), None)
    if batch is None:
        raise PublishedRefused(f"no real-cluster evaluation batch {entry.batch} is published")
    record = next((r for r in batch.records if r.run_id == entry.run_id), None)
    if record is None:
        raise PublishedRefused(f"the batch {entry.batch} holds no record of {entry.run_id}")
    return record, batch


def _consistent(
    entry: PublishedEntry,
    record: RunRecord,
    events: Sequence[TraceEvent],
    header: Mapping[str, Any],
    eval_record: EvalRecord,
) -> None:
    """The files tell one story: one run, on the declared real cluster, with its own recording."""
    if record.run_id != entry.run_id:
        raise PublishedRefused(f"run.json is run {record.run_id}, not {entry.run_id}")
    strangers = sorted({e.run_id for e in events if e.run_id != entry.run_id})
    if strangers:
        raise PublishedRefused(f"trace.jsonl holds events of another run ({strangers[0]})")

    environment = record.environment
    if environment is None:
        raise PublishedRefused("run.json records no environment")
    if environment.is_simulated or environment.kind != entry.target:
        shown = "the simulator" if environment.is_simulated else environment.kind
        raise PublishedRefused(f"the run recorded {shown}, not the declared target {entry.target}")
    first = events[0]
    started_on = first.payload.get("environment") if first.category == "state.transition" else None
    if first.payload.get("to") != "CREATED" or not isinstance(started_on, Mapping):
        raise PublishedRefused("trace.jsonl does not start with the run's creation")
    if started_on.get("kind") != entry.target or started_on.get("is_simulated") is not False:
        raise PublishedRefused("the trace's first event names another target than run.json")

    last = events[-1]
    if last.category != "run.summary" or last.payload.get("state") != record.state.value:
        raise PublishedRefused("trace.jsonl does not end with the run's final state")

    info = record.provider_info
    if info is None:
        raise PublishedRefused("run.json records no provider")
    if info.origin == "replay":
        raise PublishedRefused("a replay never runs against a real cluster: the record is wrong")
    own = Path(info.recording or "").stem
    if own != header["recording_id"]:
        raise PublishedRefused("recording.jsonl is not this run's own recording")

    decided = {
        (str(e.payload.get("approval_id")), str(e.payload.get("decision")))
        for e in events
        if e.category == "approval.decided"
    }
    for approval in record.approvals:
        if (approval.approval_id, approval.decision) not in decided:
            raise PublishedRefused(
                f"approval {approval.approval_id} is in run.json but not in the trace"
            )

    if eval_record.target != entry.target:
        raise PublishedRefused(
            f"its evaluation record names target {eval_record.target!r}, not {entry.target}"
        )
    if eval_record.final_state != record.state.value:
        raise PublishedRefused(
            f"its evaluation record ends {eval_record.final_state}, run.json {record.state.value}"
        )
    verified = bool(record.recovery and record.recovery.verified)
    if eval_record.oracle_verified != verified:
        raise PublishedRefused(
            "its evaluation record and run.json disagree on the oracle's verdict"
        )


def _without_trace(
    batches: Iterable[EvalBatch], loaded: set[str]
) -> tuple[RecordWithoutTrace, ...]:
    """Every real-cluster evaluation record with no published trace, oldest first."""
    found = [
        RecordWithoutTrace(record, batch)
        for batch in batches
        for record in batch.records
        if record.run_id not in loaded
    ]
    return tuple(sorted(found, key=lambda r: (r.record.started_at is None, r.record.started_at)))


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "the file"
    return f"{where}: {first.get('msg', 'invalid')}"[:300]
