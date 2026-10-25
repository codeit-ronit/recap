"""scripts/live_eval.py: refuses what a live evaluation must never use, summarizes truthfully."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from recap.core.config import REPO_ROOT, Settings
from recap.core.models import RunConfig
from recap.telemetry.tracer import TraceCategory as C
from scripts import live_eval
from scripts.live_eval import PlannedRun, check_plan, load_plan, main, summarize
from tests.adversarial.test_m3_adversarial import case_insensitive
from tests.e2e.test_runbook_search import Searcher, live_run
from tests.llm_fakes import FakeOllama


def plan_file(tmp_path: Path, provider: str) -> Path:
    path = tmp_path / "plan.yaml"
    path.write_text(
        yaml.safe_dump({"provider": provider, "runs": [{"id": "x", "scenario": "bad-deployment"}]})
    )
    return path


@pytest.mark.parametrize("provider", ["deterministic", "anthropic", "replay"])
async def test_only_local_or_free_models_may_run(tmp_path: Path, provider: str) -> None:
    code = await main([str(plan_file(tmp_path, provider)), "--out", str(tmp_path / "out")])
    assert code == 2  # refused before anything runs
    assert not (tmp_path / "out" / "summary.json").exists()


async def test_the_owners_own_data_folder_is_refused(tmp_path: Path) -> None:
    plan = plan_file(tmp_path, "ollama")
    code = await main(
        [str(plan), "--out", str(tmp_path / "out"), "--data-dir", str(REPO_ROOT / "data")]
    )
    assert code == 2


@pytest.mark.parametrize("data_exists", [True, False])
async def test_the_owners_own_data_folder_is_refused_under_any_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_exists: bool
) -> None:
    """DATA is data/ on a case-insensitive file system (REPO_ROOT is a scratch folder here)."""
    if not case_insensitive(tmp_path):
        pytest.skip("this file system tells DATA and data apart")
    repo = tmp_path / "repo"
    repo.mkdir()
    if data_exists:
        (repo / "data").mkdir()
    monkeypatch.setattr(live_eval, "REPO_ROOT", repo)

    async def never(*args: object) -> dict:
        raise AssertionError("a planned run started in the repository's own data folder")

    monkeypatch.setattr(live_eval, "run_one", never)
    plan = plan_file(tmp_path, "ollama")
    code = await main([str(plan), "--out", str(tmp_path / "out"), "--data-dir", str(repo / "DATA")])
    assert code == 2
    if data_exists:
        assert list((repo / "data").iterdir()) == []  # nothing was written there
    else:
        assert not (repo / "data").exists()  # and an alias it created was removed


@pytest.mark.parametrize(
    ("name", "provider"), [("m3-real-models.yaml", "ollama"), ("m3-groq-free.yaml", "groq")]
)
def test_every_shipped_plan_loads_and_checks_clean(
    settings: Settings, name: str, provider: str
) -> None:
    """Scenarios, strategies, retrieval modes, overlays and planted memories all exist, so
    a long real-model plan never stops halfway on a typo."""
    plan = load_plan(REPO_ROOT / "evals" / "plans" / name)
    assert plan.provider == provider
    assert check_plan(plan, settings) == []
    assert plan.runs[-1].false_memory  # the adversarial run, which plants a memory, is last


async def test_a_plan_with_mistakes_is_refused_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def never(*args: object) -> dict:
        raise AssertionError("a run started although the plan has mistakes")

    monkeypatch.setattr(live_eval, "run_one", never)
    runs = [
        {"id": "a", "scenario": "no-such-scenario"},
        {"id": "b", "scenario": "bad-deployment", "overlays": ["fixtures/no-such-folder"]},
        {"id": "c", "scenario": "bad-deployment", "false_memory": "fixtures/redteam/none.yaml"},
    ]
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump({"provider": "ollama", "runs": runs}))
    assert await main([str(path), "--out", str(tmp_path / "out")]) == 2
    said = capsys.readouterr().err
    assert "no-such-scenario" in said and "no-such-folder" in said and "none.yaml" in said


def test_a_plan_with_an_unknown_strategy_or_retrieval_does_not_load(tmp_path: Path) -> None:
    for run in ({"strategy": "parallell"}, {"retrieval": "dense"}):
        path = tmp_path / "plan.yaml"
        plan = {"provider": "ollama", "runs": [{"id": "x", "scenario": "bad-deployment", **run}]}
        path.write_text(yaml.safe_dump(plan))
        with pytest.raises(SystemExit, match=next(iter(run.values()))):
            load_plan(path)


@pytest.mark.e2e
async def test_the_summary_comes_from_the_run_record(settings: Settings) -> None:
    fake = FakeOllama(responder=Searcher())
    runtime, record = await live_run(settings, RunConfig(provider="ollama", use_memory=False), fake)
    events = runtime.events.list_events(record.run_id)
    summary = summarize(
        PlannedRun(id="t", scenario="bad-deployment"), record, events, 1.0, settings
    )

    assert (summary["state"], summary["origin"]) == ("RESOLVED", "local_llm")
    assert summary["model_requests"] == record.usage.model_requests == len(fake.chats)
    assert summary["input_tokens"] == record.usage.actual_input_tokens
    tools = [e.payload["tool"] for e in events if e.category == C.TOOL_CALL]
    assert sum(len(v) for v in summary["tool_calls_by_agent"].values()) == len(tools)
    assert summary["agent_searches"] and summary["agent_searches"][0]["agent"] == "investigator"
    assert (
        summary["actions"] == ["execute_rollback on checkout: recovered"] and summary["recovered"]
    )
