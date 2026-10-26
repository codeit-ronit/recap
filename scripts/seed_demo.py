"""Fill a SEPARATE data folder with real RECAP runs, so the dashboard has something to show.

    python scripts/seed_demo.py --data-dir ~/recap-demo
    RECAP_DATA_DIR=~/recap-demo uvicorn apps.api.main:app     # then open http://127.0.0.1:8000

Every run here really runs, offline, against the simulated environment, and says what made
its decisions: the scripted policy (SCRIPTED, not an LLM) or a replay of a committed
real-model recording (REPLAY: the recorded qwen3:8b's answers, no model is called now). No
fake model is used, so nothing here may be mistaken for live inference. Approvals in these
runs come from the simulation auto-approver (the dashboard's own Replay button asks a person).

Runs (in this order, so memory builds up the way a real history would):
  train runs              verified episodes in memory (scripted)
  bad-deployment          a clear single-service bad release, rolled back (scripted)
  ambiguous-incident      specialists in parallel rounds (scripted)
  memory-caution          a similar past fix rejected by live evidence (scripted)
  malicious-log           a planted instruction flagged, a destructive action denied (scripted)
  budget-exhausted        a run stopped cleanly by its budget (scripted)
  m3 unfamiliar           the held-out unfamiliar incident (scripted)
  m3 adversarial          forged signals, a poisoned runbook and a planted false memory (scripted)
  replays                 every committed recording (fixtures/recordings/MANIFEST.json)

The repository's own data/ folder is refused: demo runs never mix with real experience.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from recap.core.config import REPO_ROOT, Settings, load_settings
from recap.core.models import MemoryPartition, RunConfig, RunRecord
from recap.providers.recordings import list_fixtures
from recap.runtime import Runtime
from scripts.redteam_setup import is_repository_data
from scripts.redteam_setup import main as plant

POISONED = REPO_ROOT / "fixtures" / "knowledge-overlays" / "poisoned"
FALSE_MEMORY = REPO_ROOT / "fixtures" / "redteam" / "false-memory-checkout-restart.yaml"
AUTO = RunConfig(approval_mode="auto", label="seed-demo")

# (scenario, run settings) for the scripted runs, in order.
SCRIPTED: list[tuple[str, RunConfig]] = [
    ("train-payment-cascade", AUTO),
    ("train-checkout-bad-release", AUTO),
    ("bad-deployment", AUTO),
    ("ambiguous-incident", AUTO),
    ("memory-caution", AUTO),
    ("malicious-log", AUTO),
    ("budget-exhausted", AUTO),
    ("m3-inventory-leak-behind-frontend", AUTO.model_copy(update={"strategy": "parallel"})),
]


def with_overlays(settings: Settings, overlays: list[Path]) -> Settings:
    paths = settings.paths.model_copy(update={"knowledge_overlays": overlays})
    return settings.model_copy(update={"paths": paths})


async def seed(data_dir: Path) -> list[RunRecord]:
    settings = load_settings(data_dir=data_dir)
    runtime = Runtime(settings)
    done: list[RunRecord] = []
    for scenario, config in SCRIPTED:
        done.append(await runtime.run_incident(scenario, config))
        print(f"  {scenario}: {done[-1].state.value} (scripted)", flush=True)

    # The adversarial incident with every lure: a poisoned runbook folder for this run's
    # index, and a planted "verified" false memory (LIVE) that this run is allowed to read.
    if plant(["--data-dir", str(data_dir), "--false-memory", str(FALSE_MEMORY)]) != 0:
        raise SystemExit("could not plant the red-team memory")
    lured = Runtime(with_overlays(settings, [POISONED]))
    config = AUTO.model_copy(
        update={"memory_read_partitions": [MemoryPartition.TRAIN, MemoryPartition.LIVE]}
    )
    done.append(await lured.run_incident("m3-payment-release-under-forged-signals", config))
    print(f"  m3-payment-release-under-forged-signals: {done[-1].state.value} (scripted)")

    for fixture in list_fixtures():
        # A replay asks exactly what its recording asked: routing, memory and retrieval come
        # from the manifest (as the API's replay launcher does).
        replay = AUTO.model_copy(
            update={
                "provider": "replay",
                "replay_fixture": fixture.id,
                "strategy": fixture.strategy,
                "use_memory": fixture.use_memory,
                "retrieval": fixture.retrieval,
            }
        )
        done.append(await runtime.run_incident(fixture.scenario, replay))
        print(f"  replay {fixture.id}: {done[-1].state.value} (recorded real-model run)")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, required=True, help="a separate demo folder")
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    if is_repository_data(data_dir, REPO_ROOT):
        print("refused: use a separate folder, not the repository's own data/", file=sys.stderr)
        return 2
    if os.environ.get("RECAP_REPLAY_RECORDING"):
        print(
            "refused: unset RECAP_REPLAY_RECORDING (replays here use the manifest)", file=sys.stderr
        )
        return 2
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"seeding {data_dir.name}/ (simulated environment, offline, no model called)")
    runs = asyncio.run(seed(data_dir))
    print(f"{len(runs)} runs; start the API with RECAP_DATA_DIR pointing at this folder")
    return 0


if __name__ == "__main__":
    sys.exit(main())
