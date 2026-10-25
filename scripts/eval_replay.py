"""Replay every committed real-model recording offline, and record the replays as one batch.

    python scripts/eval_replay.py --out /tmp/recap-replay          # every manifest recording
    python scripts/eval_replay.py --out DIR --only qwen3-8b-bad-deployment
    python scripts/eval_replay.py --out DIR --publish              # also evals/reports/batches/

Each recording listed in fixtures/recordings/MANIFEST.json is checked against its pinned
SHA-256 (recap/providers/recordings.py), then replayed through RECAP's whole path: the
controller, the MCP tool server, the gateway, the simulator, the verifier, the approval and
the oracle all run now; only the model's answers come from the recording. No model is
called, nothing is sent over a network, and nothing costs anything.

Each replay runs in a fresh data folder of its own: a replay reads memory as its recording
found it, and these were recorded with an empty one. Runbook search uses only the shipped
runbooks, with the retrieval mode the manifest names (an overlay from the shell would change
what the model is shown, and the replay would stop).

The replays form one evaluation batch of mode `offline_replay` (evals/records.py), written to
--out (batch.json, batch.md) and to <data folder>/evals/batches/<batch_id>/. It shows whether
the recorded decisions still pass RECAP's current code, not how well a model does today. A
replay that leaves its recording (ReplayMismatch: a prompt, tool or schema changed since the
recording) ends FAILED with failure category `error`, and so does any crash; the script then
exits 1. Approvals come from the simulation auto-approver, standing in for whoever approved
the original run (the manifest names them): the approval comes after the model's last
answer, so it changes nothing the model saw.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from evals.categorize import (
    batch_markdown,
    clean_text,
    created_at_of,
    publish_batch,
    record_from_run,
    run_events,
)
from evals.records import EvalBatch, EvalRecord, make_batch, write_batch, write_batch_file
from recap.core.config import REPO_ROOT, Settings, load_settings
from recap.core.errors import ConfigError
from recap.core.ids import new_id, utcnow
from recap.core.models import RunConfig
from recap.providers import recordings
from recap.providers.factory import REPLAY_ENV
from recap.providers.recordings import FixtureInfo
from recap.runtime import Runtime, code_version
from scripts.redteam_setup import is_repository_data

EXIT_REFUSED = 2  # nothing was replayed
EXIT_FAILED = 1  # a recording was refused, or a replay ended with an error


def replay_settings(base: Settings, recording: Path) -> Settings:
    """The settings of one replay: the replay provider reads `recording`, and runbook search
    covers the shipped runbooks only (no overlays from the shell, no embeddings switch).

    RunConfig.replay_fixture names the same recording by its manifest id; the provider
    setting names its file too, so the replay reads it however the provider is chosen. The
    run's own record must then name the manifest's recording id (checked in replay_one).
    """
    providers = dict(base.providers.providers)
    where = _repo_relative(recording) or str(recording)  # local settings, never published
    providers["replay"] = providers["replay"].model_copy(update={"recording": where})
    return base.model_copy(
        update={
            "providers": base.providers.model_copy(update={"providers": providers}),
            "paths": base.paths.model_copy(update={"knowledge_overlays": []}),
            "features": base.features.model_copy(update={"embeddings": False}),
        }
    )


async def replay_one(info: FixtureInfo, data_dir: Path) -> EvalRecord:
    """Replay one checked recording in `data_dir` and return its evaluation record."""
    fixture = recordings.read_fixture(info.id)  # refused (ConfigError) if its bytes changed
    runtime = Runtime(replay_settings(load_settings(data_dir=data_dir), fixture.path))
    config = RunConfig.model_validate(  # the manifest's switches, validated like any run's
        {
            "provider": "replay",
            "replay_fixture": info.id,
            "strategy": info.strategy,
            "use_memory": info.use_memory,
            "retrieval": info.retrieval,
            "approval_mode": "auto",
            "label": f"eval-replay:{info.id}",
        }
    )
    record = await runtime.run_incident(info.scenario, config)
    replayed = (record.provider_info.replay_of or {}) if record.provider_info else {}
    if replayed.get("recording_id") != info.recording_id:
        raise ConfigError(
            f"{info.id}: the replay ran from recording {replayed.get('recording_id')!r}, not "
            f"the manifest's {info.recording_id!r}"
        )
    events = run_events(runtime.events, record.run_id)
    scenario = next((s for s in runtime.list_scenarios() if s.id == info.scenario), None)
    return record_from_run(
        record, events, mode="offline_replay", scenario=scenario, recording=info.file
    )


def replay_batch(
    batch_id: str, records: list[EvalRecord], refused: list[str], started: datetime
) -> EvalBatch:
    """The replays as one `offline_replay` batch, with how to read it."""
    return make_batch(
        batch_id=batch_id,
        mode="offline_replay",
        title="Offline replay of the committed real-model recordings",
        records=records,
        created_at=created_at_of(records, started),
        plan=_shown(recordings.MANIFEST),
        methods=[
            "Each recording in the manifest was checked against its pinned SHA-256, then "
            "replayed with scripts/eval_replay.py through RECAP's whole path (controller, MCP "
            "tool server, gateway, simulator, verifier, approval, oracle); only the model's "
            "answers came from the recording. No model was called; $0.",
            "Each replay ran in a fresh data folder (a replay reads memory as its recording "
            "found it: empty), with the routing, memory and retrieval its manifest entry "
            "names, over the shipped runbooks only.",
            "Approvals: the simulation auto-approver stood in for the original approver (the "
            "manifest names them); the approval comes after the model's last answer, so it "
            "changes nothing the model saw.",
            "Model requests, reported tokens and model_seconds are the replay's own: a replay "
            "sends no request, so they are 0 or empty. The recorded run's own counts are in "
            "its recording (fixtures/recordings/) and its manifest entry.",
            "Each record is built from the replay's own record and trace "
            "(evals/categorize.py): success only when the independent oracle verified the "
            "recovery; a replay that left its recording (ReplayMismatch) is category error.",
            f"RECAP code {code_version(REPO_ROOT)} ('+dirty': with uncommitted changes).",
        ],
        limitations=[
            f"N = {len(records)} recordings: a replay shows that recorded decisions still pass "
            "RECAP's current code. It is not a new sample of any model, and its outcome says "
            "nothing about how often the model would decide the same way.",
            "Each recording is one run of one model on the SIMULATED shop, not a real cluster.",
            "A replay cannot show what a model would do with a changed prompt: a changed "
            "prompt makes the replay stop (ReplayMismatch), and the recording must be made "
            "again with a real model.",
            *refused,
        ],
    )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="where the batch goes")
    parser.add_argument(
        "--data-dir", type=Path, help="the replays' data folder (default: OUT/data)"
    )
    parser.add_argument("--only", action="append", default=[], help="replay only these ids")
    parser.add_argument(
        "--publish", action="store_true", help="also publish the batch in evals/reports/batches/"
    )
    args = parser.parse_args(argv)

    if os.environ.get(REPLAY_ENV, "").strip():
        print(
            f"refused: unset {REPLAY_ENV}; this script replays each recording the manifest "
            "lists, by its id",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    data_dir = (args.data_dir or args.out / "data").resolve()
    if is_repository_data(data_dir, REPO_ROOT):
        print(
            "refused: use a separate data folder, not the repository's own data/", file=sys.stderr
        )
        return EXIT_REFUSED
    try:
        fixtures = recordings.list_fixtures()
    except ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    unknown = [name for name in args.only if name not in {f.id for f in fixtures}]
    chosen = [f for f in fixtures if not args.only or f.id in args.only]
    if unknown or not chosen:
        named = ", ".join(unknown) if unknown else "none"
        print(f"refused: no such recording in the manifest ({named})", file=sys.stderr)
        return EXIT_REFUSED

    started = utcnow()
    batch_id = f"{started:%Y%m%d-%H%M%S}-offline-replay-{new_id('batch').split('-')[-1][:6]}"
    records: list[EvalRecord] = []
    refused: list[str] = []
    for info in chosen:
        try:
            record = await replay_one(info, data_dir / batch_id / info.id)
        except ConfigError as exc:  # an altered or missing recording: nothing was replayed
            refused.append(clean_text(f"Not replayed: {info.id} ({exc})."))
            print(f"== {info.id}: refused: {exc}", file=sys.stderr)
            continue
        records.append(record)
        outcome = "success" if record.success else record.failure
        print(f"== {info.id}: {record.final_state} ({outcome}) in {record.seconds:.1f} s")
    batch = replay_batch(batch_id, records, refused, started)
    args.out.mkdir(parents=True, exist_ok=True)
    write_batch_file(batch, args.out / "batch.json")
    (args.out / "batch.md").write_text(batch_markdown(batch), encoding="utf-8")
    write_batch(batch, data_dir / "evals" / "batches")
    code = EXIT_FAILED if refused or any(r.failure == "error" for r in records) else 0
    if args.publish:
        try:
            path = publish_batch(batch)
        except ValueError as exc:
            print(f"not published: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        print(f"published {_shown(path.parent)}")
    return code


def _repo_relative(path: Path) -> str | None:
    resolved = path.resolve()
    return (
        resolved.relative_to(REPO_ROOT).as_posix() if resolved.is_relative_to(REPO_ROOT) else None
    )


def _shown(path: Path) -> str:
    """A repo-relative path, or the file's own name: local folders never reach a batch."""
    return _repo_relative(path) or path.name


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
