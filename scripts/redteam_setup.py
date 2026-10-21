"""Red-team setup: plant a false memory into a SCRATCH data folder before an adversarial run.

    python scripts/redteam_setup.py --data-dir /tmp/recap-redteam \\
        --false-memory fixtures/redteam/false-memory-checkout-restart.yaml

The episode is stored as-is in the folder's memory (its own partition, e.g. LIVE), with a
provenance note that names the fixture, so a later run in that folder finds it like any
past episode. RECAP must then reject it on that run's live evidence.

It refuses the repository's own data folder (data/): planted memories must never reach
the owner's real memory. Use a scratch folder and point the run at it (RECAP_DATA_DIR).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from recap.core.config import REPO_ROOT
from recap.core.models import ExperienceEpisode
from recap.memory.store import ExperienceStore, MemoryAccess


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", required=True, type=Path, help="a scratch data folder")
    parser.add_argument("--false-memory", required=True, type=Path, help="an episode YAML")
    args = parser.parse_args(argv)

    data_dir = args.data_dir.resolve()
    if data_dir == (REPO_ROOT / "data").resolve():
        print("refused: never plant memories in the repository's own data folder", file=sys.stderr)
        return 2
    raw = yaml.safe_load(args.false_memory.read_text(encoding="utf-8"))
    episode = ExperienceEpisode.model_validate(raw)
    access = MemoryAccess(
        readable_partitions=[episode.partition], write_partition=episode.partition
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    store = ExperienceStore(data_dir / "memory.db")
    if store.get(episode.episode_id) is not None:
        print(f"{episode.episode_id} is already planted in {data_dir}")
        return 0
    store.add(episode, access)
    print(f"planted {episode.episode_id} ({episode.partition.value}) in {data_dir / 'memory.db'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
