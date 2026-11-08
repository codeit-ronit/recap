"""Check or re-pin evals/live/MANIFEST.json, the allowlist of published real-cluster runs.

    python scripts/published_runs.py --check    # check each entry as the API does; 1 on failure
    python scripts/published_runs.py --update   # only after a deliberate change to an export

The API shows a published run (GET /published, then its trace, evidence and approvals) only
when it passes every check in evals/published.py: the folder is listed, each file's SHA-256 and
size match the pins, the record and trace match RECAP's schemas, the files agree with each other
and with the run's evaluation record, and nothing in them is shaped like a credential. --check
runs exactly those checks and writes nothing; tests/unit/test_published_runs.py does the same in
CI.

--update re-pins each listed file after an export was changed on purpose (for example a README
corrected in review). It never adds an entry: a new published run needs its title and notes
written by a person. Never use --update to silence an unexpected --check failure: an unexpected
change means a file was altered.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from evals.published import MANIFEST, check_published, load_published, refresh_pins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="check every entry; write nothing")
    mode.add_argument("--update", action="store_true", help="re-pin hashes and sizes")
    args = parser.parse_args(argv)

    if args.update:
        for change in refresh_pins(MANIFEST):
            print(f"re-pinned {change}")
    problems = check_published(MANIFEST)
    for problem in problems:
        print(f"problem: {problem}")
    if problems:
        return 1
    index = load_published(MANIFEST)
    for run in index.runs:
        print(f"ok {run.run_id} ({run.entry.target}): {run.entry.folder}")
    without = len(index.records_without_trace)
    print(f"{len(index.runs)} published runs load; {without} real-cluster records have no trace")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
