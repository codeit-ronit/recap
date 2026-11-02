"""The Kind adapter's local state (recap/environments/kubernetes/state.py).

Never in the cluster, never shown to a model: the per-run file (executor key, used nonces,
release log, write and command audits), the namespace lease and the write switch. The
nonce is committed BEFORE the cluster is touched (fail closed), audits are append-only, and
reading never creates a file.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from recap.core.errors import CapabilityError
from recap.core.observations import AuthorizedAction
from recap.environments.kubernetes.kubectl import CommandRecord
from recap.environments.kubernetes.state import (
    NOT_RESET,
    LeaseHeld,
    NamespaceLease,
    RunState,
    disable_writes,
    enable_writes,
    writes_disabled,
)
from recap.safety.capabilities import CapabilityIssuer, verify_capability

RUN = "run-state0001"
ACTION = AuthorizedAction(
    action="restart", target="backend", approval_id="apr-1", proposal_digest="d" * 64
)


def ready_state(tmp_path: Path) -> RunState:
    state = RunState(tmp_path / f"kind-{RUN}.db")
    state.start(run_id=RUN, scenario_id="kind-bad-release", seed=1, namespace="recap-demo")
    state.put(
        reset_complete=True,
        baselines={"backend": {"version": "v1.0.0", "revision": 2}},
        baseline_at="2026-11-02T10:00:00+00:00",
        observation_start="2026-11-02T09:58:00+00:00",
    )
    return state


def signed(issuer: CapabilityIssuer) -> AuthorizedAction:
    token = issuer.issue(
        action="restart",
        target="backend",
        params={},
        approval_id="apr-1",
        proposal_digest="d" * 64,
    )
    return ACTION.model_copy(update={"capability": token})


def verifier(action: AuthorizedAction):  # type: ignore[no-untyped-def]
    def verify(key: str | None, used: set[str]):  # type: ignore[no-untyped-def]
        return verify_capability(
            action.capability, public_key=key, run_id=RUN, action=action, used_nonces=used
        )

    return verify


def test_reading_never_creates_the_file(tmp_path: Path) -> None:
    state = RunState(tmp_path / "kind-x.db")
    assert state.meta() == {} and state.release_log() == [] and state.writes() == []
    assert state.used_nonces() == set() and state.commands() == []
    with pytest.raises(RuntimeError, match=NOT_RESET):
        state.info()
    with pytest.raises(CapabilityError, match="no executor key is bound"):
        state.consume_capability(verifier(ACTION))
    assert not state.path.exists()


def test_a_key_can_be_bound_only_after_a_complete_reset(tmp_path: Path) -> None:
    state = RunState(tmp_path / "kind-x.db")
    state.start(run_id=RUN)
    with pytest.raises(RuntimeError, match=NOT_RESET):
        state.bind_key("key")
    state.put(
        reset_complete=True,
        baseline_at="2026-11-02T10:00:00+00:00",
        observation_start="2026-11-02T10:00:00+00:00",
    )
    state.bind_key("key")
    assert state.get("executor_key") == "key"
    info = state.info()
    assert info.baseline_at == datetime(2026, 11, 2, 10, tzinfo=UTC)


def test_the_nonce_is_committed_before_anything_else_happens(tmp_path: Path) -> None:
    state = ready_state(tmp_path)
    issuer = CapabilityIssuer(RUN, "inc-1")
    state.bind_key(issuer.public_key)
    action = signed(issuer)

    payload = state.consume_capability(verifier(action))

    # Committed: another process (a fresh object on the same file) sees it at once, so a
    # write that crashes or fails afterwards can never be retried with this capability.
    assert payload.nonce in RunState(state.path).used_nonces()
    with pytest.raises(CapabilityError, match="already used"):
        RunState(state.path).consume_capability(verifier(action))


def test_a_refused_capability_records_no_nonce(tmp_path: Path) -> None:
    state = ready_state(tmp_path)
    state.bind_key(CapabilityIssuer(RUN, "inc-1").public_key)
    forged = signed(CapabilityIssuer(RUN, "inc-1"))  # signed with another key

    with pytest.raises(CapabilityError, match="signature"):
        state.consume_capability(verifier(forged))
    assert state.used_nonces() == set()


def test_audits_are_append_only(tmp_path: Path) -> None:
    state = ready_state(tmp_path)
    issuer = CapabilityIssuer(RUN, "inc-1")
    state.bind_key(issuer.public_key)
    state.consume_capability(verifier(signed(issuer)))  # one used nonce
    state.record_write(
        action="restart",
        target="backend",
        approval_id="apr-1",
        proposal_digest="d" * 64,
        outcome="executed",
        detail={"generation_before": 3},
    )
    state.record_command(CommandRecord("writer", "rollout_restart", ["kubectl"], 0, 12, 300, ""))
    state.log_release("backend", "v1.0.0", "v1.0.0", "restart")

    for table in ("writes", "commands", "releases", "nonces"):
        with (
            sqlite3.connect(state.path) as conn,
            pytest.raises(sqlite3.DatabaseError, match="append-only"),
        ):
            conn.execute(f"DELETE FROM {table}")
    with (
        sqlite3.connect(state.path) as conn,
        pytest.raises(sqlite3.DatabaseError, match="append-only"),
    ):
        conn.execute("UPDATE writes SET outcome = 'refused'")
    assert state.writes()[0]["detail"] == {"generation_before": 3}
    assert state.commands()[0]["argv"] == ["kubectl"]


def test_a_new_reset_keeps_the_used_nonces_and_forgets_the_key(tmp_path: Path) -> None:
    state = ready_state(tmp_path)
    issuer = CapabilityIssuer(RUN, "inc-1")
    state.bind_key(issuer.public_key)
    payload = state.consume_capability(verifier(signed(issuer)))

    state.start(run_id=RUN, scenario_id="again")

    assert payload.nonce in state.used_nonces()
    assert state.get("executor_key") is None and state.get("reset_complete") is None


# --- the namespace lease -----------------------------------------------------------------------


def test_one_run_at_a_time_holds_the_namespace(tmp_path: Path) -> None:
    lease = NamespaceLease(tmp_path / "kind-leases.db")
    now = datetime(2026, 11, 2, 10, tzinfo=UTC)
    assert lease.holder("recap-demo") is None and not lease.path.exists()

    lease.take("recap-demo", "run-a", 600, now=now)
    lease.take("recap-demo", "run-a", 600, now=now + timedelta(seconds=5))  # renew: fine
    with pytest.raises(LeaseHeld, match="leased by run run-a"):
        lease.take("recap-demo", "run-b", 600, now=now + timedelta(seconds=10))
    assert lease.release("recap-demo", "run-b") is False  # only its holder may release it
    holder = lease.holder("recap-demo")
    assert holder is not None and holder.run_id == "run-a"

    lease.take("recap-demo", "run-b", 600, now=now + timedelta(seconds=700))  # expired: taken
    assert lease.holder("recap-demo").run_id == "run-b"  # type: ignore[union-attr]
    assert lease.release("recap-demo", "run-b") is True and lease.holder("recap-demo") is None


# --- the write switch --------------------------------------------------------------------------


def test_the_write_switch_is_a_file_people_can_see(tmp_path: Path) -> None:
    switch = tmp_path / "sandbox" / "kind-writes-disabled"
    assert not writes_disabled(switch)
    disable_writes(switch, "incident review")
    assert writes_disabled(switch) and "incident review" in switch.read_text()
    enable_writes(switch)
    enable_writes(switch)  # already on: no error
    assert not writes_disabled(switch)
