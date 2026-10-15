"""Run the reproducible V1 evaluation (baselines + ablations + red-team) on the simulator.

    python scripts/evaluate.py                    # full protocol, writes evals/reports/latest/
    python scripts/evaluate.py --configs full_adaptive baseline_single

Results are synthetic, simulator-only measurements with a small N (see the report caveats).
The benchmark is defined for the scripted default policy; any other provider is refused.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from evals.runner import LATEST_DIR, new_report_dir, run_evaluation, update_latest
from recap.core.errors import ConfigError


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--configs", nargs="*", default=None, help="only these configuration names")
    parser.add_argument("--read-latency-ms", type=int, default=30)
    parser.add_argument("--write-latency-ms", type=int, default=50)
    parser.add_argument(
        "--no-latest", action="store_true", help="do not update evals/reports/latest"
    )
    args = parser.parse_args()

    out_dir = new_report_dir()
    print(f"RECAP V1 evaluation (SIMULATED) → {out_dir}")
    try:
        report = await run_evaluation(
            out_dir,
            config_names=args.configs,
            read_latency_ms=args.read_latency_ms,
            write_latency_ms=args.write_latency_ms,
        )
    except ConfigError as exc:  # e.g. a mistyped --configs name; nothing has run
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.no_latest:
        if update_latest(report, out_dir):
            print(f"copied report files to {LATEST_DIR}")
        else:
            print(f"no test or red-team case ran, so {LATEST_DIR} was left unchanged")
    print()
    print((out_dir / "summary.md").read_text(encoding="utf-8").split("## Per-case outcomes")[0])
    print(f"{len(report.cases)} test/red-team cases + {len(report.train)} train runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
