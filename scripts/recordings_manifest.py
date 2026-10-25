"""Check or refresh fixtures/recordings/MANIFEST.json, the pins of every committed recording.

    python scripts/recordings_manifest.py --check    # verify every entry; exit 1 on any problem
    python scripts/recordings_manifest.py --update   # only after a deliberate re-recording

A committed recording (a replay fixture) is content-addressed: the manifest pins its SHA-256
and its size, and a replay chosen by manifest id refuses a file whose bytes differ
(recap/providers/recordings.py). --check verifies that every entry names a file in
fixtures/recordings/ whose bytes match the pin, that the file parses as a recording whose
header repeats the entry's facts (recording ID, provider, model, prompt version), that its
export folder exists, and that every recording in the folder has an entry. It writes
nothing. CI runs the same check (tests/unit/test_recordings_manifest.py).

--update is for the owner, after re-recording a fixture ON PURPOSE: it re-pins each listed
file's SHA-256 and size, copies the facts of its header, prints every change and checks
again. It never adds an entry (a new recording needs a title and a description written by
a person), and it leaves an entry's prose alone, which still describes the earlier run:
review it before committing. Never use --update to silence an unexpected --check failure:
an unexpected change means the recording was altered.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from recap.core.errors import ConfigError
from recap.providers import recordings
from recap.providers.transport import shown_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify every entry; write nothing")
    mode.add_argument(
        "--update", action="store_true", help="re-pin hashes and sizes after a re-recording"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="another manifest (default: fixtures/recordings/MANIFEST.json)",
    )
    args = parser.parse_args(argv)
    manifest: Path = args.manifest or recordings.MANIFEST
    shown = shown_path(manifest)

    if args.update:
        try:
            changes = recordings.refresh_manifest(manifest)
        except ConfigError as exc:
            print(f"not updated: {exc}")
            return 1
        for change in changes:
            print(f"updated {change}")
        if changes:
            print(
                "Review each changed entry's title, description, agents and approved_by: "
                "they describe the run that was recorded."
            )
        else:
            print(f"{shown}: every pin already matches; nothing was written")

    problems = recordings.check_manifest(manifest)
    for problem in problems:
        print(f"problem: {problem}")
    if problems:
        print(f"{shown}: {len(problems)} problem(s)")
        return 1
    count = len(recordings.list_fixtures(manifest))
    print(f"{shown}: {count} recording(s), every one matches its pin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
