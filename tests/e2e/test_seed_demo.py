"""scripts/seed_demo.py fills a separate folder with real runs, and none of them pretends.

The dashboard's demo data must be honest: every run's decisions came from the scripted
policy or from a replay of a committed real-model recording (no fake model, no live
inference), and the repository's own data/ folder is refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recap.core.config import REPO_ROOT
from recap.harness.store import RunStore
from scripts.seed_demo import main

pytestmark = pytest.mark.e2e


def test_the_repository_data_folder_is_refused() -> None:
    assert main(["--data-dir", str(REPO_ROOT / "data")]) == 2


def test_the_demo_runs_are_scripted_or_replays_of_real_recordings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECAP_USAGE_LEDGER", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("RECAP_TOOL_LATENCY_MS", "0")
    monkeypatch.delenv("RECAP_REPLAY_RECORDING", raising=False)
    data = tmp_path / "demo"
    assert main(["--data-dir", str(data)]) == 0

    runs = RunStore(data / "recap.db").list(limit=100)
    origins = {r.provider_info.origin for r in runs if r.provider_info is not None}
    assert origins == {"scripted", "replay"}  # never a live or fake model
    states = {r.scenario_id: r.state.value for r in runs if r.provider_info.origin == "scripted"}
    assert states["malicious-log"] == "BLOCKED" and states["budget-exhausted"] == "BUDGET_EXHAUSTED"
    replays = [r for r in runs if r.provider_info.origin == "replay"]
    assert len(replays) == 2 and all(r.state.value == "RESOLVED" for r in replays)
    assert {r.config.replay_fixture for r in replays} == {
        "qwen3-8b-unfamiliar-parallel",
        "qwen3-8b-bad-deployment",
    }
