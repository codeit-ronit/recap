"""The evaluation runner keeps its leakage protocol, writes its report and refuses bad input.

One small evaluation (the train phase, then `baseline_single` on every test scenario) runs
once for this module, end to end through the real MCP server, into a temporary directory.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.runner import (
    EvalConfig,
    EvalReport,
    Split,
    _caveats,
    _scored_config,
    load_eval_configs,
    load_split,
    run_evaluation,
    update_latest,
)
from recap.core.errors import ConfigError
from recap.core.models import MemoryPartition, ProviderInfo
from recap.harness.store import RunStore
from recap.memory.store import ExperienceStore
from recap.runtime import Runtime


def quiet(message: str) -> None:
    """A progress callback that prints nothing."""


@pytest.fixture(scope="module")
def evaluation(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, EvalReport]:
    out_dir = tmp_path_factory.mktemp("eval") / "report"
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("RECAP_PROVIDER", raising=False)  # the benchmark needs the scripted default
        report = asyncio.run(
            run_evaluation(
                out_dir,
                config_names=["baseline_single"],
                read_latency_ms=0,
                write_latency_ms=0,
                progress=quiet,
            )
        )
    return out_dir, report


# --- the leakage protocol -------------------------------------------------------------------


@pytest.mark.e2e
def test_memory_holds_one_train_episode_per_train_scenario_and_nothing_else(
    evaluation: tuple[Path, EvalReport],
) -> None:
    out_dir, report = evaluation
    episodes = ExperienceStore(out_dir / "data" / "memory.db").list()
    assert sorted(e.scenario_id for e in episodes) == sorted(load_split().train)
    assert {e.partition for e in episodes} == {MemoryPartition.TRAIN}
    assert {e.origin_run_id for e in episodes} == {c.run_id for c in report.train}


@pytest.mark.e2e
def test_scored_runs_read_train_memory_only_and_write_nothing(
    evaluation: tuple[Path, EvalReport],
) -> None:
    out_dir, report = evaluation
    runs = RunStore(out_dir / "data" / "recap.db")
    for case in report.cases:
        record = runs.get(case.run_id)
        assert record is not None
        assert record.episode_id is None  # nothing was consolidated into memory
        assert record.config.memory_read_partitions == [MemoryPartition.TRAIN]
        assert record.config.memory_write is False
        # MemoryAccess.eval_test labels writes TEST, which the store refuses even if forced.
        assert record.config.memory_write_partition == MemoryPartition.TEST
        assert record.config.approval_mode == "auto"


def test_a_yaml_cannot_turn_memory_writes_on_while_scoring() -> None:
    cheat = EvalConfig(
        name="cheat",
        description="tries to write test episodes",
        suite="test",
        config={"memory_write": True, "memory_read_partitions": ["train", "demo"]},
    )
    config = _scored_config(cheat)
    assert config.memory_write is False
    assert config.memory_read_partitions == [MemoryPartition.TRAIN]


# --- the report -----------------------------------------------------------------------------


@pytest.mark.e2e
def test_the_report_scores_every_test_scenario_and_is_written_to_disk(
    evaluation: tuple[Path, EvalReport],
) -> None:
    out_dir, report = evaluation
    split = load_split()
    assert [c.scenario_id for c in report.train] == split.train
    assert [c.scenario_id for c in report.cases] == split.test
    assert len(report.cases) == 6 and {c.config for c in report.cases} == {"baseline_single"}
    assert [s.name for s in report.summaries] == ["baseline_single"]
    for name in ("results.json", "results.csv", "summary.md"):
        assert (out_dir / name).is_file()
    assert "auto-approver" in (out_dir / "summary.md").read_text(encoding="utf-8")


def test_caveats_count_the_split_instead_of_a_fixed_number() -> None:
    split = Split(name="tiny", train=["a"], test=["b", "c"], redteam=[])
    assert any("N is tiny (2 test scenarios, 0 red-team scenarios)" in c for c in _caveats(split))


def test_an_empty_report_never_replaces_the_latest_report(
    evaluation: tuple[Path, EvalReport], tmp_path: Path
) -> None:
    out_dir, report = evaluation
    latest = tmp_path / "latest"
    assert update_latest(report.model_copy(update={"cases": []}), out_dir, latest) is False
    assert not latest.exists()
    assert update_latest(report, out_dir, latest) is True
    assert sorted(p.name for p in latest.iterdir()) == ["results.csv", "results.json", "summary.md"]


# --- refusals -------------------------------------------------------------------------------


def test_eval_files_with_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        EvalConfig.model_validate(
            {"name": "x", "description": "d", "suite": "test", "confg": {"use_rag": False}}
        )
    with pytest.raises(ValidationError):
        Split.model_validate({"name": "x", "train": [], "test": [], "red_team": ["y"]})


def test_requested_configurations_load_in_file_order_and_typos_are_refused() -> None:
    names = [c.name for c in load_eval_configs(["no_rag", "baseline_single"])]
    assert names == ["baseline_single", "no_rag"]
    with pytest.raises(ConfigError, match="baseline_singel"):
        load_eval_configs(["baseline_singel", "no_rag"])


async def test_an_unknown_configuration_is_refused_before_anything_is_created(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="nope"):
        await run_evaluation(tmp_path / "report", config_names=["nope"], progress=quiet)
    assert not (tmp_path / "report").exists()


async def test_only_the_scripted_policy_can_be_evaluated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeLLMProvider:
        info = ProviderInfo(name="fake-llm", kind="llm")

    monkeypatch.setattr(Runtime, "make_provider", lambda self, config: FakeLLMProvider())
    progress: list[str] = []
    with pytest.raises(ConfigError, match="scripted policy"):
        await run_evaluation(
            tmp_path / "report", config_names=["baseline_single"], progress=progress.append
        )
    assert progress == []  # refused before any scenario ran
