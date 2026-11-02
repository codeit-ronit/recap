"""The console approval on a live target (M5 plan, section 4.2: no anonymous approval).

On a target that is not simulated, such as the local Kind cluster, a plain "y" is a
rejection: the operator must type the target service's exact name. The decision stays bound
to the exact proposal digest, and the decision log keeps this machine's login name
(getpass.getuser()) as local evidence, in a table of its own that no trace, API response or
export reads. The simulator keeps "y" / "yes", exactly as before.

Why a table of its own: the export's last gate (scripts/export_run_trace.py) looks for a
one-word login name ("bob") only where a path would hold it, since such a name is also an
ordinary word (tests/unit/test_export_gates.py pins this). A login written into the decision
log's text would therefore reach the README and the trace of an export unseen.
"""

from __future__ import annotations

import getpass
import importlib.util
import io
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import EnvironmentConfig, Settings
from recap.core.errors import ApprovalError
from recap.core.models import (
    ApprovalRequest,
    ApprovalStatus,
    EnvironmentInfo,
    IncidentState,
    MitigationProposal,
    RunConfig,
)
from recap.runtime import Runtime
from recap.safety import approvals
from recap.safety.approvals import ApprovalStore, ConsoleApprovalChannel
from tests.fakes import FakeEnvironment

KIND = EnvironmentInfo(
    kind="kind",
    name="local test cluster",
    namespace="demo",
    is_simulated=False,
    supported_actions=["rollback", "restart"],
    description="local Kind cluster, not production",
)
SIM = EnvironmentInfo(kind="simulated", name="test simulator", namespace="shop", is_simulated=True)
LOGIN = "opsrunner"  # one word: the export gate would not see it outside a path
ANSWERED = r"answered after \d+\.\d s( \(faster than a person can read the proposal\))?"
LIVE_PROMPT = (
    "LIVE target (kind): type the target service's name to approve this exact action "
    "(anything else rejects): "
)


@pytest.fixture(autouse=True)
def login_name(monkeypatch: pytest.MonkeyPatch) -> str:
    """The operating-system login the console records (never the developer's own)."""
    monkeypatch.setattr(getpass, "getuser", lambda: LOGIN)
    return LOGIN


@pytest.fixture
def store() -> ApprovalStore:
    return ApprovalStore()  # in-memory SQLite


def make_proposal(target: str = "api", **params: object) -> MitigationProposal:
    return MitigationProposal(
        proposal_id="prop-1",
        incident_id="inc-1",
        action="execute_rollback",
        target=target,
        params=params or {"to_version": "v3.1.0"},
        rationale="errors started right after the newest release",
        evidence_ids=["ev-1-001"],
    )


class Answers:
    """A console: the proposal lines it printed, the prompts it showed, one typed answer."""

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.prompts: list[str] = []
        self.printed: list[str] = []

    def input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.answer is None:
            raise EOFError
        return self.answer

    def channel(
        self, store: ApprovalStore, environment: EnvironmentInfo | None = KIND
    ) -> ConsoleApprovalChannel:
        return ConsoleApprovalChannel(
            store, input_fn=self.input, output_fn=self.printed.append, environment=environment
        )


async def decide_at_console(
    store: ApprovalStore,
    answer: str | None,
    environment: EnvironmentInfo | None = KIND,
    request: ApprovalRequest | None = None,
) -> Answers:
    """Open a request for make_proposal() (unless one is given) and answer it at the console."""
    if request is None:
        request = store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)
    console = Answers(answer)
    await console.channel(store, environment).notify(request)
    return console


def decision_note(store: ApprovalStore) -> str:
    return store.events("prop-1")[-1].detail


# --- a live target: only the target service's exact name approves ----------------------------


async def test_typing_the_target_services_name_approves_the_exact_proposal(
    store: ApprovalStore,
) -> None:
    proposal = make_proposal()
    console = await decide_at_console(store, "api")

    [approval] = store.approvals_for("inc-1")
    assert (approval.decision, approval.actor) == ("approved", "console-user")
    # Still bound to the exact proposal: its digest, and single use.
    assert approval.proposal_digest == proposal.digest()
    assert approval.consumed_at is None
    assert console.prompts == [LIVE_PROMPT]
    shown = "\n".join(console.printed)
    assert "LIVE TARGET: kind (local Kind cluster, not production), not a simulation" in shown
    for expected in ["execute_rollback", "api", "v3.1.0", "ev-1-001", proposal.digest()[:12]]:
        assert expected in shown
    assert re.fullmatch(
        r"typed the target service's name; "
        + ANSWERED
        + "; login name kept in the local decision log",
        decision_note(store),
    )
    store.consume(approval.approval_id)  # the gateway can use it, once
    with pytest.raises(ApprovalError, match="single use"):
        store.consume(approval.approval_id)


@pytest.mark.parametrize(
    "answer",
    [
        "y",
        "yes",
        " YES ",
        "",
        "Api",  # the name, but not exactly
        "web",  # another service
        "api please",
        "execute_rollback",
        None,  # input ended (EOF)
    ],
)
async def test_anything_but_the_exact_name_rejects_on_a_live_target(
    store: ApprovalStore, answer: str | None
) -> None:
    await decide_at_console(store, answer)

    [approval] = store.approvals_for("inc-1")
    assert (approval.decision, approval.actor) == ("rejected", "console-user")
    assert store.get_request("prop-1").status == ApprovalStatus.REJECTED
    assert decision_note(store).startswith("did not type the target service's name; answered")


async def test_spaces_around_the_name_are_not_part_of_it(store: ApprovalStore) -> None:
    await decide_at_console(store, "  api  ")
    assert store.approvals_for("inc-1")[0].decision == "approved"


async def test_a_live_rejection_revokes_an_approval_that_was_not_used_yet(
    store: ApprovalStore,
) -> None:
    """ "No" wins until the approval is used, and the login is kept with the revocation."""
    proposal = make_proposal()
    request = store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    store.decide(
        "prop-1", decision="approved", proposal_digest=proposal.digest(), actor="operator:alice"
    )
    await decide_at_console(store, "y", request=request)  # "y" is a "no" on a live target

    revoked = store.events("prop-1")[-1]
    assert (revoked.kind, revoked.actor) == ("revoked", "console-user")
    assert revoked.detail.endswith("overrides the approval by operator:alice before it was used")
    assert store.local_logins("prop-1") == {revoked.event_id: LOGIN}


# --- the decision stays bound to the exact proposal digest -------------------------------------


async def test_a_request_showing_another_proposal_than_its_digest_is_rejected_unasked(
    store: ApprovalStore,
) -> None:
    """The name is checked against the proposal shown, so it must be the one the digest binds."""
    request = store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)
    shown = request.proposal.model_copy(update={"target": "web"})
    forged = request.model_copy(update={"proposal": shown})  # the digest is still api's

    console = await decide_at_console(store, "web", request=forged)

    assert console.prompts == []  # nobody was asked to type a name for it
    [approval] = store.approvals_for("inc-1")
    assert approval.decision == "rejected"
    assert decision_note(store).startswith(
        "rejected without asking: the proposal shown does not match its digest"
    )


async def test_a_typed_name_cannot_approve_a_proposal_the_store_does_not_hold(
    store: ApprovalStore,
) -> None:
    held = store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)
    other = make_proposal(to_version="v2.0.0")  # same service, another restore point
    swapped = held.model_copy(update={"proposal": other, "proposal_digest": other.digest()})

    with pytest.raises(ApprovalError, match="digest mismatch"):
        await decide_at_console(store, "api", request=swapped)

    assert store.get_request("prop-1").status == ApprovalStatus.PENDING
    refused = store.events("prop-1")[-1]
    assert (refused.kind, refused.actor) == ("refused", "console-user")
    assert store.local_logins("prop-1") == {refused.event_id: LOGIN}  # who tried, kept locally


# --- what is recorded, and where ---------------------------------------------------------------


async def test_the_login_name_is_local_evidence_never_in_the_decision_logs_text(
    store: ApprovalStore,
) -> None:
    await decide_at_console(store, "api")

    decided = store.events("prop-1")[-1]
    assert decided.kind == "approved"
    assert store.local_logins("prop-1") == {decided.event_id: LOGIN}
    # Traces, the API (/approvals/.../events) and exports read only these entries.
    readable = [*store.events("prop-1"), *store.recent_events()]
    assert not any(LOGIN in f"{e.actor} {e.detail}" for e in readable)
    assert all(LOGIN not in a.actor for a in store.approvals_for("inc-1"))


async def test_a_live_request_with_no_login_name_is_rejected_without_asking(
    store: ApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_login() -> str:
        raise OSError("no login name")

    monkeypatch.setattr(getpass, "getuser", no_login)
    console = await decide_at_console(store, "api")

    assert console.prompts == []
    assert store.approvals_for("inc-1")[0].decision == "rejected"
    assert decision_note(store) == (
        "rejected without asking: no login name to record for a live target"
    )
    assert store.local_logins("prop-1") == {}


async def test_piped_input_on_a_live_target_is_rejected_unread(
    store: ApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    piped = io.StringIO("api\n")  # `echo api | python scripts/demo.py --approve ask`
    monkeypatch.setattr(sys, "stdin", piped)
    request = store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)
    channel = ConsoleApprovalChannel(store, output_fn=lambda *a: None, environment=KIND)
    await channel.notify(request)  # the real input()

    assert store.approvals_for("inc-1")[0].decision == "rejected"
    assert piped.read() == "api\n"  # never read
    assert decision_note(store) == (
        "rejected unread: no interactive terminal; login name kept in the local decision log"
    )


def test_the_login_record_is_append_only_like_the_decision_log(tmp_path: Path) -> None:
    db = tmp_path / "recap.db"
    store = ApprovalStore(db)
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    store.decide(
        "prop-1",
        decision="approved",
        proposal_digest=proposal.digest(),
        actor="console-user",
        local_login=LOGIN,
    )
    [(event_id, login)] = store.local_logins("prop-1").items()

    raw = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("UPDATE approval_logins SET login = 'someone else'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("DELETE FROM approval_logins")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute(
            "INSERT OR REPLACE INTO approval_logins VALUES (?, 'prop-1', 'mallory')", (event_id,)
        )
    raw.close()
    assert store.local_logins("prop-1") == {event_id: login} == {event_id: LOGIN}


def test_a_decision_log_from_before_m5_gains_the_login_table(tmp_path: Path) -> None:
    db = tmp_path / "recap.db"
    before_m5 = approvals._SCHEMA.split("-- The operating-system login name")[0]
    old = sqlite3.connect(db)
    old.executescript(before_m5)
    old.close()

    store = ApprovalStore(db)  # opening it adds the table; nothing else changes
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    store.decide(
        "prop-1",
        decision="approved",
        proposal_digest=proposal.digest(),
        actor="console-user",
        local_login=LOGIN,
    )
    assert list(store.local_logins("prop-1").values()) == [LOGIN]
    assert [e.kind for e in store.events("prop-1")] == ["requested", "approved"]


# --- typed-ahead input is discarded --------------------------------------------------------------


class Terminal(io.TextIOBase):
    """Standard input on a terminal. What was typed before the prompt waits in the terminal's
    input queue until it is flushed; the answer typed after the prompt comes next."""

    FD = 97  # not 0, so input() reads through readline() instead of the C prompt

    def __init__(self, typed_ahead: str, typed_after: str) -> None:
        self.queue = [typed_ahead] if typed_ahead else []
        self.after = typed_after

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self.FD

    def readable(self) -> bool:
        return True

    def readline(self, size: int | None = -1) -> str:
        if self.queue:
            return self.queue.pop(0)
        line, self.after = self.after, ""
        return line


@pytest.mark.parametrize(
    ("typed_ahead", "typed_after", "decision"),
    [
        ("api\n", "", "rejected"),  # the right name, typed before the prompt: it does not count
        ("y\n", "api\n", "approved"),  # an early "y" is dropped; the name typed after counts
    ],
)
async def test_input_typed_before_the_prompt_is_discarded(
    store: ApprovalStore,
    monkeypatch: pytest.MonkeyPatch,
    typed_ahead: str,
    typed_after: str,
    decision: str,
) -> None:
    termios = pytest.importorskip("termios")
    terminal = Terminal(typed_ahead, typed_after)
    flushed: list[tuple[int, int]] = []

    def tcflush(fd: int, queue: int) -> None:  # what the kernel does for TCIFLUSH
        flushed.append((fd, queue))
        terminal.queue.clear()

    monkeypatch.setattr(sys, "stdin", terminal)
    monkeypatch.setattr(termios, "tcflush", tcflush)
    request = store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)
    channel = ConsoleApprovalChannel(store, output_fn=lambda *a: None, environment=KIND)
    await channel.notify(request)  # the real input() and the real _discard_typed_ahead()

    assert flushed == [(Terminal.FD, termios.TCIFLUSH)]
    assert store.approvals_for("inc-1")[0].decision == decision
    assert decision_note(store).startswith("interactive terminal; ")


# --- the simulator path is unchanged ------------------------------------------------------------


@pytest.mark.parametrize("environment", [SIM, None], ids=["simulated", "not-given"])
@pytest.mark.parametrize(
    ("answer", "decision"),
    [("y", "approved"), (" YES ", "approved"), ("api", "rejected"), ("n", "rejected")],
)
async def test_the_simulator_keeps_y_or_yes(
    store: ApprovalStore, environment: EnvironmentInfo | None, answer: str, decision: str
) -> None:
    console = await decide_at_console(store, answer, environment=environment)

    assert store.approvals_for("inc-1")[0].decision == decision
    assert console.prompts == ["Approve this exact action? [y/N] "]
    assert not any("LIVE" in line for line in console.printed)
    assert re.fullmatch(ANSWERED, decision_note(store))
    assert store.local_logins("prop-1") == {}  # no login is read or kept for a simulation
    assert console.channel(store, environment).live is False


# --- an exported live run carries no login name ------------------------------------------------


class LiveLookingEnvironment(FakeEnvironment):
    """The tests' second adapter, reporting itself as a real target of kind "live"."""

    def describe(self) -> EnvironmentInfo:
        return super().describe().model_copy(update={"is_simulated": False, "kind": "live"})


def load_export_script() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "export_run_trace.py"
    spec = importlib.util.spec_from_file_location("export_run_trace", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.e2e
@pytest.mark.parametrize("login", [LOGIN, "ops.runner"], ids=["one-word", "several-parts"])
async def test_an_export_of_a_live_run_approved_at_the_console_holds_no_login_name(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, login: str
) -> None:
    """One word: the gate would miss it in text. Several parts: the gate would refuse the
    whole export. Either way the export is written, and the name is in neither."""
    monkeypatch.setattr(getpass, "getuser", lambda: login)
    live = settings.model_copy(
        update={
            "environment": EnvironmentConfig(
                kind="tests.unit.test_console_approval_live:LiveLookingEnvironment",
                options={"state_dir": str(settings.paths.data_dir / "live")},
            )
        }
    )
    runtime = Runtime(live)
    console = Answers("checkout")  # the fake target's broken service, typed by name
    channel = ConsoleApprovalChannel(
        runtime.approvals,
        input_fn=console.input,
        output_fn=console.printed.append,
        environment=runtime.environment_info(),  # what scripts/demo.py has to pass
    )
    record = await runtime.run_incident(
        "fake-bad-release", RunConfig(memory_write=False), approval_channel=channel
    )

    assert record.state == IncidentState.RESOLVED, record.outcome_note
    assert console.prompts and console.prompts[0].startswith("LIVE target (live): type")
    [approval] = record.approvals
    assert approval.actor == "console-user"
    assert list(runtime.approvals.local_logins(approval.proposal_id).values()) == [login]

    export = load_export_script()
    out = tmp_path / "export"
    written = export.export_run(live, record.run_id, out)  # the last gate passes

    assert written == ["trace.jsonl", "run.json", "README.md"]
    texts = {name: (out / name).read_text(encoding="utf-8") for name in written}
    for name, text in texts.items():
        for spelled in {login, login.replace(".", ""), login.replace(".", "-")}:
            assert spelled.lower() not in text.lower(), name
    # The export still says how the approval was given, and that a login was kept locally.
    assert "typed the target service's name" in texts["README.md"]
    assert "login name kept in the local decision log" in texts["trace.jsonl"]
