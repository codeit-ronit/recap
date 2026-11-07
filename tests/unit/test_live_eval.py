"""scripts/live_eval.py: refuses what a live evaluation must never use, summarizes truthfully,
and writes each plan run as one `real_llm` evaluation batch (with its cost controls).

No test here calls a real model: a fake Ollama server (tests/llm_fakes.py) answers through
the real provider code, or the planned run itself is replaced.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from evals.records import EvalRecord, load_batch
from recap.core.config import REPO_ROOT, Settings
from recap.core.models import RunConfig, RunRecord
from recap.harness.store import RunStore
from recap.providers.factory import create_provider
from recap.providers.protocol import PROMPT_VERSION
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from scripts import live_eval
from scripts.live_eval import (
    EXIT_REFUSED,
    EXIT_STOPPED,
    PlannedRun,
    check_plan,
    load_plan,
    main,
    running_in_ci,
    summarize,
    with_benchmark_verdicts,
)
from tests.adversarial.test_m3_adversarial import case_insensitive
from tests.e2e.test_runbook_search import Searcher, live_run
from tests.llm_fakes import FakeOllama, ScriptedModel


@pytest.fixture(autouse=True)
def not_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI services set CI=true while they run this suite, and a live evaluation refuses to
    run there: every test starts outside CI, and the CI test sets it itself."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("RECAP_ALLOW_LIVE_FREE_INFERENCE", raising=False)


def plan_file(tmp_path: Path, provider: str, *run_ids: str) -> Path:
    path = tmp_path / "plan.yaml"
    runs = [{"id": run_id, "scenario": "bad-deployment"} for run_id in run_ids or ("x",)]
    path.write_text(yaml.safe_dump({"provider": provider, "runs": runs}))
    return path


async def never(*args: object) -> Any:
    raise AssertionError("a planned run started")


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
    ("name", "provider"),
    [
        ("m3-real-models.yaml", "ollama"),
        ("m3-groq-free.yaml", "groq"),
        ("m4-comparisons.yaml", "ollama"),
    ],
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


# --- cost controls -------------------------------------------------------------------------------


def test_the_ci_variable_counts_whenever_it_holds_a_value() -> None:
    assert running_in_ci({"CI": "true"}) and running_in_ci({"CI": "1"})
    assert not running_in_ci({}) and not running_in_ci({"CI": "  "})


async def test_nothing_live_runs_in_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(live_eval, "run_one", never)
    code = await main([str(plan_file(tmp_path, "ollama")), "--out", str(tmp_path / "out")])
    assert code == EXIT_REFUSED
    assert "never run in CI" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()  # refused before anything was written


async def test_a_hosted_free_plan_needs_consent_for_each_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(live_eval, "run_one", never)
    plan = str(plan_file(tmp_path, "groq"))
    assert await main([plan, "--out", str(tmp_path / "out")]) == EXIT_REFUSED
    assert "--confirm-free-plan" in capsys.readouterr().err
    # The flag is consent for this batch, not a key to the plan: the free-tier guard's own
    # checks (the opt-in variable, the key, the dated confirmation) still come first.
    assert await main([plan, "--out", str(tmp_path / "out"), "--confirm-free-plan"]) == 2
    assert "RECAP_ALLOW_LIVE_FREE_INFERENCE" in capsys.readouterr().err
    assert not (tmp_path / "out" / "summary.json").exists()


def evaluated(run_id: str, **fields: Any) -> EvalRecord:
    """A planned run's evaluation record, as run_one returns it."""
    base: dict[str, Any] = {
        "run_id": run_id,
        "mode": "real_llm",
        "scenario_id": "bad-deployment",
        "provider": "groq:openai/gpt-oss-120b",
        "provider_class": "free_hosted_llm",
        "model": "openai/gpt-oss-120b",
        "prompt_version": "prompts-v4.7",
    }
    return EvalRecord.model_validate({**base, **fields})


def summary_of(run_id: str, state: str) -> dict[str, Any]:
    """The summary fields main() prints and summary.md shows."""
    return {
        "id": run_id,
        "scenario": "bad-deployment",
        "run_id": run_id,
        "state": state,
        "outcome_note": "",
        "seconds": 1.0,
        "model": "openai/gpt-oss-120b",
        "origin": "free_hosted_llm",
        "weights": None,
        "prompt_version": "prompts-v4.7",
        "recording": None,
        "model_requests": 2,
        "input_tokens": 900,
        "output_tokens": 120,
        "rounds": [],
        "tool_calls_by_agent": {},
        "verification": None,
        "agent_searches": [],
        "memory_hits": [],
        "actions": [],
        "recovered": state == "RESOLVED",
    }


@pytest.mark.parametrize(
    ("stopped", "reason"),
    [
        ({"final_state": "QUOTA_EXHAUSTED", "failure": "quota_cap"}, "quota"),
        (
            {
                "final_state": "FAILED",
                "failure": "provider_failure",
                "failure_detail": "FreeStatusLost: x-ratelimit-limit-requests changed",
            },
            "tripwire",
        ),
    ],
)
async def test_a_quota_stop_ends_the_batch_and_starts_no_further_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stopped: dict[str, Any],
    reason: str,
) -> None:
    """The run the stop hit is recorded; the plan's later runs never start."""
    monkeypatch.setenv("RECAP_ALLOW_LIVE_FREE_INFERENCE", "1")
    monkeypatch.setattr(live_eval.Runtime, "make_provider", lambda self, config: None)
    started: list[str] = []
    outcomes = {
        "first": (summary_of("first", "RESOLVED"), evaluated("run-first", success=True)),
        "second": (summary_of("second", stopped["final_state"]), evaluated("run-2", **stopped)),
    }

    async def fake_run(base: Settings, plan: object, run: PlannedRun) -> Any:
        started.append(run.id)
        return outcomes[run.id]

    monkeypatch.setattr(live_eval, "run_one", fake_run)
    out = tmp_path / "out"
    plan = str(plan_file(tmp_path, "groq", "first", "second", "third"))
    code = await main([plan, "--out", str(out), "--confirm-free-plan"])

    assert code == EXIT_STOPPED and started == ["first", "second"]
    assert "not started: third" in capsys.readouterr().err
    batch = load_batch(out / "batch.json")
    assert [r.run_id for r in batch.records] == ["run-first", "run-2"]
    assert batch.records[1].failure == stopped["failure"]
    assert any("not started: third" in line for line in batch.limitations)
    assert len(json.loads((out / "summary.json").read_text())) == 2
    # The same batch is kept in the data folder, where the dashboard's local batches live.
    local = out / "data" / "evals" / "batches" / batch.batch_id / "batch.json"
    assert load_batch(local) == batch


# --- the batch of a real plan run, end to end with a fake model server --------------------------


@pytest.mark.e2e
async def test_every_plan_run_is_one_real_llm_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole script: the plan's run goes through RECAP with a fake Ollama server, and
    the batch says what the run record says."""
    fake = FakeOllama(responder=ScriptedModel())

    def fake_provider(self: Runtime, config: RunConfig) -> Any:
        return create_provider("ollama", self.settings, mock_http=fake.mock(), ledger=self.ledger)

    monkeypatch.setattr(Runtime, "make_provider", fake_provider)
    out = tmp_path / "out"
    assert await main([str(plan_file(tmp_path, "ollama", "first")), "--out", str(out)]) == 0

    batch = load_batch(out / "batch.json")
    [record] = batch.records
    assert (batch.mode, batch.plan, batch.models) == ("real_llm", "plan.yaml", ["qwen3:8b"])
    assert (record.success, record.failure, record.provider_class) == (True, "", "local_llm")
    assert record.model_requests == len(fake.chats) > 0
    assert record.input_tokens == 1200 * len(fake.chats)  # as the fake server reported them
    assert record.recording and "/" not in record.recording  # a file name, never a path
    assert record.approvals == ["approved by auto-approver[simulation]"]
    [summary] = json.loads((out / "summary.json").read_text())
    assert summary["run_id"] == record.run_id and summary["state"] == record.final_state
    assert (out / "batch.md").read_text().startswith("# Real model on simulated incidents")
    assert any("N = 1" in line for line in batch.limitations)


async def test_a_plan_without_any_run_to_start_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(live_eval, "run_one", never)
    plan = str(plan_file(tmp_path, "ollama"))
    assert await main([plan, "--out", str(tmp_path / "out"), "--only", "typo"]) == EXIT_REFUSED
    assert "no run of the plan is named typo" in capsys.readouterr().err


# --- rebuilding a batch from runs already made ----------------------------------------------------


async def test_a_batch_is_rebuilt_from_the_runs_in_a_data_folder(
    settings: Settings, tmp_path: Path
) -> None:
    """--from-data reads the finished real-model runs; scripted runs in the folder are not
    real-model runs and are left out. No model is called."""
    fake = FakeOllama(responder=ScriptedModel())
    _, record = await live_run(settings, RunConfig(provider="ollama", use_memory=False), fake)
    await Runtime(settings).run_incident("bad-deployment", RunConfig(approval_mode="auto"))
    asked = len(fake.chats)
    out = tmp_path / "rebuilt"
    data = str(settings.paths.data_dir)
    assert await main(["--from-data", "--data-dir", data, "--out", str(out)]) == 0

    assert len(fake.chats) == asked  # no request was sent
    batch = load_batch(out / "batch.json")
    assert [r.run_id for r in batch.records] == [record.run_id]
    assert batch.batch_id == f"{batch.created_at:%Y-%m-%d}-real-llm-qwen3-8b-{PROMPT_VERSION}"
    assert batch.plan is None and any("--from-data" in line for line in batch.methods)
    assert (settings.paths.data_dir / "evals" / "batches" / batch.batch_id / "batch.json").is_file()


async def test_a_rebuild_needs_runs_of_one_model_and_one_prompt_version(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = str(tmp_path / "out")
    assert await main(["--from-data", "--out", out]) == EXIT_REFUSED  # which folder?
    empty = tmp_path / "empty"
    empty.mkdir()
    assert await main(["--from-data", "--data-dir", str(empty), "--out", out]) == EXIT_REFUSED

    fake = FakeOllama(responder=ScriptedModel())
    _, record = await live_run(settings, RunConfig(provider="ollama", use_memory=False), fake)
    store = RunStore(settings.paths.recap_db)
    assert record.provider_info is not None
    other = record.provider_info.model_copy(update={"prompt_version": "prompts-v0.0"})
    store.save(record.model_copy(update={"run_id": "run-otherpv001", "provider_info": other}))
    capsys.readouterr()
    data = str(settings.paths.data_dir)
    assert await main(["--from-data", "--data-dir", data, "--out", out]) == EXIT_REFUSED
    assert "more than one model or prompt version" in capsys.readouterr().err


async def test_a_rebuild_with_a_plan_keeps_its_runs_and_names_the_missing_ones(
    settings: Settings, tmp_path: Path
) -> None:
    fake = FakeOllama(responder=ScriptedModel())
    labelled = RunConfig(provider="ollama", use_memory=False, label="live-eval:first")
    _, record = await live_run(settings, labelled, fake)
    await live_run(settings, RunConfig(provider="ollama", use_memory=False), fake)  # unplanned
    plan = str(plan_file(tmp_path, "ollama", "first", "second"))
    out = tmp_path / "out"
    data = str(settings.paths.data_dir)
    assert await main([plan, "--from-data", "--data-dir", data, "--out", str(out)]) == 0
    batch = load_batch(out / "batch.json")
    assert [r.run_id for r in batch.records] == [record.run_id]
    assert batch.plan == "plan.yaml"  # outside the repository: its name, never its folder
    assert "Planned but not in this folder: second." in batch.limitations


async def test_runs_on_a_real_cluster_make_a_real_kubernetes_batch(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """M5: runs against the Kind target are their own mode, read with that target's profile,
    and never share a batch with the simulator's runs."""
    fake = FakeOllama(responder=ScriptedModel())
    _, first = await live_run(settings, RunConfig(provider="ollama", use_memory=False), fake)
    _, second = await live_run(settings, RunConfig(provider="ollama", use_memory=False), fake)
    store = RunStore(settings.paths.recap_db)

    def on_the_cluster(record: RunRecord) -> RunRecord:
        assert record.environment is not None
        cluster = record.environment.model_copy(update={"kind": "kind", "is_simulated": False})
        return record.model_copy(update={"environment": cluster, "scenario_id": "kind-bad-release"})

    out = str(tmp_path / "out")
    data = str(settings.paths.data_dir)
    store.save(on_the_cluster(first))
    assert await main(["--from-data", "--data-dir", data, "--out", out]) == EXIT_REFUSED
    assert "more than one target (the simulator, kind)" in capsys.readouterr().err
    store.save(on_the_cluster(second))
    assert await main(["--from-data", "--data-dir", data, "--out", out]) == EXIT_REFUSED
    assert "RECAP_TARGET=kind-demo" in capsys.readouterr().err  # its scenarios are unknown here

    monkeypatch.setenv("RECAP_TARGET", "kind-demo")
    note = "Both runs were rehearsals."
    assert await main(["--from-data", "--data-dir", data, "--out", out, "--limitation", note]) == 0
    batch = load_batch(Path(out) / "batch.json")
    assert batch.mode == "real_kubernetes"
    assert [r.run_id for r in batch.records] == [first.run_id, second.run_id]
    assert batch.batch_id == (  # the cluster in the id: a SREGym batch of the same day differs
        f"{batch.created_at:%Y-%m-%d}-real-kubernetes-kind-qwen3-8b-{PROMPT_VERSION}"
    )
    assert batch.title.startswith("Real model on a real Kubernetes cluster (kind): qwen3:8b")
    assert batch.records[0].expected["fix_params"] == {"to_version": "v1.0.0"}  # Kind's answer
    assert note in batch.limitations
    assert any("real Kubernetes cluster (Kind" in line for line in batch.limitations)
    assert any(line.startswith("The Kind demo: two demo services") for line in batch.limitations)
    assert batch.targets == ["kind"] and {r.target for r in batch.records} == {"kind"}
    assert batch.benchmark is None and {r.benchmark_verdict for r in batch.records} == {None}
    assert not any("SIMULATED shop" in line for line in batch.limitations)
    assert any("cannot be replayed" in line for line in batch.limitations)  # refused when live


def test_a_batch_says_whether_a_person_reviewed_its_fixes() -> None:
    """The limitation follows who approved: the simulator, a person, or nobody was asked."""
    auto = evaluated("run-a", approvals=["approved by auto-approver[simulation]"])
    person = evaluated("run-p", approvals=["approved by console-user (interactive terminal)"])
    unasked = evaluated("run-u")
    assert "do not test human review" in live_eval._review_limitation([auto, unasked])
    assert "someone other than" in live_eval._review_limitation([person])
    assert "1 of the 2" in live_eval._review_limitation([auto, person])
    assert "no run tested the approval step" in live_eval._review_limitation([unasked])


def test_a_benchmarks_own_verdicts_are_recorded_apart_from_recaps() -> None:
    """M5: SREGym's grader gives the benchmark's verdict, recorded by hand per run; it never
    becomes RECAP's success. A verdict for a run the batch does not hold is refused, and so
    is a run given twice: the last one never silently wins."""
    records = [
        EvalRecord(run_id="run-a", mode="real_kubernetes", scenario_id="s", success=False),
        EvalRecord(run_id="run-b", mode="real_kubernetes", scenario_id="s", success=True),
    ]
    graded = with_benchmark_verdicts(records, ["run-a=fail", "run-b=pass"], "the grader")
    assert [(r.benchmark_verdict, r.success) for r in graded] == [("fail", False), ("pass", True)]
    for verdicts, benchmark, why in [
        (["run-a=pass"], None, "needs --benchmark"),
        (["run-a=maybe"], "g", "RUN=pass or RUN=fail"),
        (["run-z=pass"], "g", "no such run in this batch: run-z"),
        (["run-a=pass", "run-b=pass", "run-a=fail"], "g", "run-a: one verdict per run"),
        (["run-b=pass", "run-b=pass"], "g", "run-b: one verdict per run"),
    ]:
        with pytest.raises(ValueError, match=why):
            with_benchmark_verdicts(records, verdicts, benchmark)


async def test_a_run_given_two_verdicts_refuses_the_whole_batch(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command line refuses it before anything is written: no batch with either verdict."""
    fake = FakeOllama(responder=ScriptedModel())
    _, run = await live_run(settings, RunConfig(provider="ollama", use_memory=False), fake)
    out = tmp_path / "out"
    data = str(settings.paths.data_dir)
    argv = ["--from-data", "--data-dir", data, "--out", str(out), "--benchmark", "the grader"]
    for verdict in ("pass", "fail"):
        argv += ["--benchmark-verdict", f"{run.run_id}={verdict}"]

    assert await main(argv) == EXIT_REFUSED
    assert f"refused: {run.run_id}: one verdict per run (given pass, then fail)" in (
        capsys.readouterr().err
    )
    assert not (out / "batch.json").exists()
