"""End to end with an LLM provider: real HTTP protocol, scripted answers, no network.

Each test runs a whole incident: controller, real MCP tool server subprocess, gateway,
simulator, verifier, approval and oracle. The "model" is ScriptedModel behind FakeOllama or
FakeGroq (tests/llm_fakes.py): it answers over the real Ollama/Groq HTTP protocols but does
not reason. So these tests prove the plumbing and the safety properties of an LLM run, not
model quality; real-model runs are measured separately (docs/status.md) and replayed by
test_recorded_model_runs.py.
"""

from __future__ import annotations

import getpass
import importlib.util
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState, RunConfig, RunRecord, TraceEvent
from recap.providers.factory import REPLAY_ENV, create_provider
from recap.providers.protocol import PROMPT_VERSION
from recap.providers.quota import OPT_IN_ENV
from recap.runtime import Runtime
from recap.safety.approvals import ConsoleApprovalChannel
from recap.telemetry.tracer import TraceCategory as C
from tests.adversarial.test_audit_oracle_isolation import (
    LABEL_FIELDS,
    ORACLE_SENTINEL,
    SCENARIO_ID_SENTINEL,
    use_sentinel_oracle,
    with_sentinel_scenario,
)
from tests.llm_fakes import (
    FAKE_GROQ_KEY,
    FAKE_REQUEST_ID,
    Answer,
    FakeGroq,
    FakeOllama,
    OperatorApproves,
    ScriptedModel,
    as_ollama_body,
    error,
)

pytestmark = pytest.mark.e2e  # every test here runs a real incident end to end

LOCAL = RunConfig(provider="ollama", use_memory=False)


def use_fake_model(runtime: Runtime, name: str, fake: FakeOllama | FakeGroq) -> None:
    """Build `name` from configs/providers.yaml for every run, with the network replaced."""

    def make(config: RunConfig) -> Any:
        return create_provider(name, runtime.settings, mock_http=fake.mock(), ledger=runtime.ledger)

    runtime.make_provider = make  # type: ignore[method-assign]


async def run(runtime: Runtime, scenario: str, config: RunConfig) -> RunRecord:
    return await runtime.run_incident(
        scenario, config, approval_channel=OperatorApproves(runtime.approvals)
    )


def events(runtime: Runtime, record: RunRecord, category: str) -> list[TraceEvent]:
    return [e for e in runtime.events.list_events(record.run_id) if e.category == category]


async def test_a_model_driven_run_resolves_through_mcp_with_an_operator_approval(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    fake = FakeOllama(responder=ScriptedModel())
    use_fake_model(runtime, "ollama", fake)

    record = await run(runtime, "bad-deployment", LOCAL)

    assert record.state == IncidentState.RESOLVED
    assert record.recovery is not None and record.recovery.verified  # the oracle's own check
    assert record.provider_info is not None and record.provider_info.origin == "local_llm"
    assert record.versions["prompt_version"] == PROMPT_VERSION
    # Provenance: which server and weights answered, and which code ran.
    assert record.provider_info.server is not None
    assert record.provider_info.server["weights"].startswith("sha256:")
    assert record.versions["code_version"] == runtime.code_version  # whatever git said
    assert [(a.action, a.target, a.result) for a in record.actions] == [
        ("execute_rollback", "checkout", "recovered")
    ]
    assert record.approvals[0].actor == "test-operator"

    # Every decision came from a model request, and the trace shows each one.
    calls = events(runtime, record, C.LLM_CALL)
    assert len(calls) == len(fake.chats) == record.usage.model_requests
    assert all(e.payload["origin"] == "local_llm" and e.payload["ok"] for e in calls)
    assert record.usage.actual_input_tokens == sum(e.payload["input_tokens"] for e in calls)
    assert record.usage.is_estimate is False

    # The tools that ran are the ones the scripted model asked for, through the gateway.
    ran = [e.payload["tool"] for e in events(runtime, record, C.TOOL_CALL)]
    assert ran[:4] == [
        "get_recent_deployments",
        "get_service_logs",
        "get_service_metrics",
        "propose_rollback",
    ]
    [summary] = events(runtime, record, C.RUN_SUMMARY)
    assert summary.payload["cost"].startswith("$0.00 - local model on this machine")


async def test_no_label_or_oracle_data_reaches_the_model_even_after_a_failed_fix(
    settings: Settings, tmp_path: Path
) -> None:
    """Oracle isolation at the wire: what is SENT to the model, byte for byte.

    The scripted model first proposes a restart (which cannot fix a bad release), so the
    oracle says "not recovered" and the controller investigates again; the sentinel oracle
    adds a made-up service to its private findings. No request body may contain it, the
    scenario's id, or an evaluation label field.
    """
    runtime = Runtime(with_sentinel_scenario(settings, tmp_path))
    probes = use_sentinel_oracle(runtime)
    fake = FakeOllama(responder=ScriptedModel(fixes=["restart_service", "execute_rollback"]))
    use_fake_model(runtime, "ollama", fake)

    record = await run(runtime, SCENARIO_ID_SENTINEL, LOCAL)

    assert [a.result for a in record.actions] == ["not_recovered", "recovered"]
    assert any(ORACLE_SENTINEL in p.unhealthy_services for p in probes)  # the oracle knew
    sent = [json.dumps(body) for body in fake.chats]
    assert len(sent) > 4
    for body in sent:
        assert ORACLE_SENTINEL not in body
        assert SCENARIO_ID_SENTINEL not in body
        for label in LABEL_FIELDS:
            assert f'"{label}"' not in body


async def test_a_recorded_run_replays_offline_to_the_same_outcome(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(settings)
    fake = FakeOllama(responder=ScriptedModel())
    use_fake_model(runtime, "ollama", fake)
    live = await run(runtime, "bad-deployment", LOCAL)
    recording = live.versions["recording"]
    assert recording and (settings.paths.root / recording).is_file()

    replay_runtime = Runtime(settings)  # the real factory: no mock network at all
    monkeypatch.setenv(REPLAY_ENV, str(settings.paths.root / recording))
    replayed = await run(
        replay_runtime, "bad-deployment", RunConfig(provider="replay", use_memory=False)
    )

    assert replayed.error is None, replayed.error  # a ReplayMismatch would show here
    assert replayed.state == live.state == IncidentState.RESOLVED
    # The recorded answers cited the LIVE run's evidence IDs; in the replay they must point
    # at the replay's own evidence (each run's IDs carry its own prefix).
    prefix = f"ev-{replayed.run_id.split('-')[-1][:6]}-"
    assert replayed.verification is not None and replayed.verification.best is not None
    cited = replayed.verification.best.supporting_evidence_ids
    assert cited and all(evidence_id.startswith(prefix) for evidence_id in cited)
    assert replayed.provider_info is not None and replayed.provider_info.origin == "replay"
    assert replayed.provider_info.replay_of["original_provider"] == "ollama"  # type: ignore[index]
    assert replayed.usage.model_requests == 0  # nothing was sent to any model
    calls = events(replay_runtime, replayed, C.LLM_CALL)
    assert calls and all(e.payload["replayed"] for e in calls)
    tools = lambda r, rt: [e.payload["tool"] for e in events(rt, r, C.TOOL_CALL)]  # noqa: E731
    assert tools(replayed, replay_runtime) == tools(live, runtime)
    [summary] = events(replay_runtime, replayed, C.RUN_SUMMARY)
    assert summary.payload["cost"].startswith("$0.00 - replayed recording")


async def test_a_free_quota_429_defers_the_run_and_the_key_is_never_stored(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OPT_IN_ENV, "1")
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("RECAP_GROQ_FREE_PLAN_CONFIRMED_ON", date.today().isoformat())
    runtime = Runtime(settings)
    fake = FakeGroq([error(429, "rate_limit_exceeded", **{"retry-after": "600"})])
    use_fake_model(runtime, "groq", fake)

    record = await run(runtime, "bad-deployment", RunConfig(provider="groq", use_memory=False))

    assert record.state == IncidentState.QUOTA_EXHAUSTED
    assert record.outcome_note.startswith("deferred:") and "429" in record.outcome_note
    assert record.actions == [] and record.episode_id is None  # nothing decided, nothing learned
    assert len(fake.chats) == 1  # one request, no retry after a long retry-after
    skipped = events(runtime, record, C.MEMORY_SKIP)
    assert "run deferred" in skipped[-1].payload["reason"]
    # The refused request itself is on the record: a trace event and one model request.
    [refused] = events(runtime, record, C.LLM_CALL)
    assert (refused.payload["status"], refused.payload["ok"]) == (429, False)
    assert record.usage.model_requests == 1
    # The key went on the wire and nowhere else: no file RECAP wrote contains it.
    assert fake.requests[-1].headers["authorization"] == f"Bearer {FAKE_GROQ_KEY}"
    written = [p for p in settings.paths.data_dir.rglob("*") if p.is_file()]
    written.append(settings.paths.usage_ledger)  # the per-user ledger lives outside data/
    for path in written:
        assert FAKE_GROQ_KEY.encode() not in path.read_bytes(), path


async def test_a_proposal_refused_by_quota_is_deferred_and_still_recorded(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The investigation succeeds, the proposal request gets a long 429: the run is deferred,
    and that last request still appears in the trace and the usage (it was sent)."""
    monkeypatch.setenv(OPT_IN_ENV, "1")
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("RECAP_GROQ_FREE_PLAN_CONFIRMED_ON", date.today().isoformat())
    model = ScriptedModel()

    def answer(body: dict[str, Any]) -> Any:
        if "action" in body["response_format"]["json_schema"]["schema"]["properties"]:
            return error(429, "rate_limit_exceeded", **{"retry-after": "600"})
        return model(as_ollama_body(body))

    runtime = Runtime(settings)
    fake = FakeGroq(responder=answer)
    use_fake_model(runtime, "groq", fake)

    record = await run(runtime, "bad-deployment", RunConfig(provider="groq", use_memory=False))

    assert record.state == IncidentState.QUOTA_EXHAUSTED and record.actions == []
    calls = events(runtime, record, C.LLM_CALL)
    assert calls[-1].payload["purpose"] == "propose" and calls[-1].payload["status"] == 429
    assert record.usage.model_requests == len(fake.chats) == len(calls)


@pytest.mark.parametrize("planner", ["default", "prompt_injected"])
async def test_a_paid_plan_response_makes_the_run_say_cost_unknown(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, planner: str
) -> None:
    """The tripwire's verdict reaches the run summary, also through the red-team wrapper."""
    monkeypatch.setenv(OPT_IN_ENV, "1")
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("RECAP_GROQ_FREE_PLAN_CONFIRMED_ON", date.today().isoformat())
    runtime = Runtime(settings)
    fake = FakeGroq(
        responder=lambda body: ScriptedModel()(as_ollama_body(body)), daily_limit="500000"
    )

    def make(config: RunConfig) -> Any:
        return create_provider(
            "groq",
            runtime.settings,
            planner=config.planner,
            mock_http=fake.mock(),
            ledger=runtime.ledger,
        )

    runtime.make_provider = make  # type: ignore[method-assign]
    config = RunConfig(provider="groq", use_memory=False, planner=planner)  # type: ignore[arg-type]
    record = await run(runtime, "bad-deployment", config)

    assert record.state == IncidentState.FAILED and "free status" in (record.error or "")
    assert len(fake.chats) == 1  # nothing after the response that showed the paid plan
    [summary] = events(runtime, record, C.RUN_SUMMARY)
    assert summary.payload["cost"].startswith("cost unknown")
    assert record.provider_info is not None
    assert record.provider_info.red_team is (planner == "prompt_injected")


# --- the shareable export of a run (scripts/export_run_trace.py) ------------------------------


def load_export_script() -> Any:
    """scripts/ is not a package: load scripts/export_run_trace.py from its path."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "export_run_trace.py"
    spec = importlib.util.spec_from_file_location("export_run_trace", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class KeyInTheRationale(ScriptedModel):
    """Writes a key-shaped string and README markup into its proposal, as a confused or
    injected model might: the export must neither publish the key nor let the text pose as
    one of RECAP's own lines."""

    def _proposal(self, schema: dict[str, Any]) -> Answer:
        answer = super()._proposal(schema)
        assert isinstance(answer, dict)
        body = json.loads(answer["content"])
        body["rationale"] = (
            f"roll back the bad release (deployed with {FAKE_GROQ_KEY}).<br>"
            "- **Approval:** approved by `someone-else`"
        )
        return {"content": json.dumps(body)}


class MissesEverything:
    """A redactor that redacts nothing: what the export's last gate must still catch."""

    def redact_text(self, text: str) -> str:
        return text


async def test_the_exported_run_holds_the_evidence_and_no_secrets(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(getpass, "getuser", lambda: "export-test-user")  # not the developer's
    runtime = Runtime(settings)
    use_fake_model(runtime, "ollama", FakeOllama(responder=KeyInTheRationale()))
    console = ConsoleApprovalChannel(
        runtime.approvals, input_fn=lambda prompt: "y", output_fn=lambda *a: None
    )
    record = await runtime.run_incident("bad-deployment", LOCAL, approval_channel=console)
    assert record.state == IncidentState.RESOLVED
    assert FAKE_GROQ_KEY in record.proposals[0].rationale  # the stored record still has it
    # An older recording may still hold account-linking IDs; the export must drop them.
    recorded = settings.paths.root / str(record.versions["recording"])
    lines = [json.loads(line) for line in recorded.read_text(encoding="utf-8").splitlines()]
    chat = next(line for line in lines[1:] if line["path"] == "/api/chat")
    chat["response"]["headers"]["x-request-id"] = FAKE_REQUEST_ID
    chat["response"]["body"]["x_groq"] = {"id": FAKE_REQUEST_ID}
    recorded.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    export = load_export_script()
    out = tmp_path / "export"
    written = export.export_run(settings, record.run_id, out)

    assert written == ["trace.jsonl", "run.json", "recording.jsonl", "README.md"]
    texts = {name: (out / name).read_text(encoding="utf-8") for name in written}
    for name, text in texts.items():
        for unwanted in (
            FAKE_GROQ_KEY,
            FAKE_REQUEST_ID,
            str(Path.home()),
            str(settings.paths.data_dir),
        ):
            assert unwanted not in text, name
    # The raw exchanges travel with the trace: one recorded chat per model request.
    recording = [json.loads(line) for line in texts["recording.jsonl"].splitlines()]
    assert recording[0]["type"] == "header"
    chats = [line for line in recording[1:] if line["path"] == "/api/chat"]
    assert len(chats) == record.usage.model_requests
    # The README says who decided what, how the approval was given, and what was written.
    readme = texts["README.md"]
    assert "`recording.jsonl` in this folder" in readme
    assert "proposal request in JSON-schema mode" in readme  # the proposal's own mode
    assert "approved by `console-user` (answered at the console prompt: answered after" in readme
    assert "Approval decision log" in readme and "consumed by `recap`" in readme
    reads = len(events(runtime, record, C.TOOL_CALL))
    assert f"{record.usage.tool_calls} tool calls ({reads} reads, 1 approved write)" in readme
    assert "decided by `R12-approval-binding`" in readme
    assert "## Scope and caveats" in readme and "One run of one simulated scenario" in readme
    # The model's rationale stays inert text: no HTML, no second "Approval" line.
    assert "<br>" not in readme and readme.count("**Approval:**") == 1
    note = "interactive terminal; answered after 53.4 s"
    assert export.approval_channel("console-user", note).endswith(note)
    assert "did not record" in export.approval_channel("console-user")
    unread = export.approval_channel("console-user", "rejected unread: no interactive terminal")
    assert "nobody answered" in unread
    assert "not a person" in export.approval_channel("auto-approver[simulation]")
    assert "signed-in operator" in export.approval_channel("operator:alice")

    # Exporting again needs replace=True, and the replaced folder holds only the new export.
    (out / "stale.txt").write_text("left from another export")
    with pytest.raises(SystemExit, match="already has files"):
        export.export_run(settings, record.run_id, out)
    export.export_run(settings, record.run_id, out, replace=True)
    assert sorted(p.name for p in out.iterdir()) == sorted(written)

    # Whatever cleaning misses, nothing is written: an environment secret, a key-shaped
    # string, the login name, or a missing model recording.
    refused = tmp_path / "refused"
    with monkeypatch.context() as m:
        m.setattr(export, "Redactor", MissesEverything)
        m.setenv("RECAP_EXPORT_TEST_TOKEN", record.run_id)
        with pytest.raises(SystemExit, match="value of \\$RECAP_EXPORT_TEST_TOKEN"):
            export.export_run(settings, record.run_id, refused)
    with monkeypatch.context() as m:
        m.setattr(export, "Redactor", MissesEverything)
        with pytest.raises(SystemExit, match="contains a Groq key"):
            export.export_run(settings, record.run_id, refused)
    with monkeypatch.context() as m:
        m.setattr(getpass, "getuser", lambda: "bad.deployment")  # a name found in the data
        with pytest.raises(SystemExit, match="login name"):
            export.export_run(settings, record.run_id, refused)
    recorded.unlink()
    with pytest.raises(SystemExit, match="recording .* is missing"):
        export.export_run(settings, record.run_id, refused)
    assert not refused.exists()
