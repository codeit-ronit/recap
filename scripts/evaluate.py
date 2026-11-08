"""Run the reproducible V1 evaluation (baselines + ablations + red-team) on the simulator.

    python scripts/evaluate.py                    # full protocol, writes evals/reports/latest/
    python scripts/evaluate.py --configs full_adaptive baseline_single
    # M6 W4: the W4 split (its own configurations), one-switch comparisons, hybrid search from
    # stored query vectors; its batch is published, the V1 report in latest/ is left as it is
    python scripts/evaluate.py --split evals/splits/w4.yaml --no-latest --publish \
        --comparisons evals/comparisons/w4.yaml --query-vectors evals/datasets/query-vectors-v1.json

Results are synthetic, simulator-only measurements with a small N (see the report caveats).
The benchmark is defined for the scripted default policy; any other provider is refused.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from evals.categorize import publish_batch
from evals.records import load_batch
from evals.runner import (
    BATCH_FILE,
    DEFAULT_SPLIT,
    LATEST_DIR,
    new_report_dir,
    run_evaluation,
    update_latest,
)
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
    parser.add_argument("--split", type=Path, default=DEFAULT_SPLIT, help="the split file")
    parser.add_argument(
        "--comparisons", type=Path, help="one-switch comparisons to check and measure"
    )
    parser.add_argument("--seeds", type=int, nargs="+", help="run every scored case once per seed")
    parser.add_argument(
        "--query-vectors", type=Path, help="stored query vectors (hybrid retrieval, offline)"
    )
    parser.add_argument(
        "--capture-query-vectors",
        action="store_true",
        help="embed the queries the stored file lacks with the local Ollama embedder and add "
        "them to --query-vectors (a capture run, not the measurement)",
    )
    parser.add_argument("--out", type=Path, help="the report folder (default: a new one)")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="with --no-latest: still publish the batch in evals/reports/batches/",
    )
    args = parser.parse_args()
    if args.capture_query_vectors and args.query_vectors is None:
        parser.error("--capture-query-vectors needs --query-vectors (the file to fill)")

    out_dir = args.out or new_report_dir()
    print(f"RECAP V1 evaluation (SIMULATED) → {out_dir}")
    try:
        report = await run_evaluation(
            out_dir,
            config_names=args.configs,
            read_latency_ms=args.read_latency_ms,
            write_latency_ms=args.write_latency_ms,
            split_path=args.split,
            comparisons_path=args.comparisons,
            seeds=args.seeds,
            query_vectors_path=args.query_vectors,
            capture_query_vectors=args.capture_query_vectors,
        )
    except ConfigError as exc:  # e.g. a mistyped --configs name; nothing has run
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.capture_query_vectors:
        print("a capture run is not a measurement: evals/reports/latest was left unchanged")
    elif args.no_latest and args.publish and report.cases:
        path = publish_batch(load_batch(out_dir / BATCH_FILE))
        print(f"published {path.parent.name} (evals/reports/latest left unchanged)")
    elif not args.no_latest:
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
