"""Tests for execution capabilities (audit finding 2.5): the write boundary's own check.

A capability is valid only for exactly one action, in one run, once, for a short time, and
only if it was signed with the key the controller bound. Every way of getting that wrong
must be refused, and the simulator must change nothing when it refuses.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from recap.core.config import REPO_ROOT
from recap.core.errors import CapabilityError
from recap.core.ids import utcnow
from recap.core.observations import AuthorizedAction
from recap.environments.simulated import SimulatedEnvironment
from recap.safety.capabilities import CapabilityIssuer, verify_capability
from tests.helpers import issuer_for, sign

RUN = "run-cap0000001"
ROLLBACK = AuthorizedAction(
    action="rollback",
    target="checkout",
    params={"to_version": "v2.4.0"},
    approval_id="apr-1",
    proposal_digest="d" * 64,
)


def issuer() -> CapabilityIssuer:
    return CapabilityIssuer(RUN, "inc-1")


def check(token: str, key: str, action: AuthorizedAction = ROLLBACK, **kwargs: object) -> None:
    verify_capability(token, public_key=key, run_id=RUN, action=action, **kwargs)  # type: ignore[arg-type]


def token_for(signer: CapabilityIssuer, action: AuthorizedAction = ROLLBACK, **kwargs: bool) -> str:
    return sign(signer, action, **kwargs).capability


# --- what a valid capability allows -----------------------------------------------------------


def test_a_capability_allows_exactly_the_signed_action() -> None:
    signer = issuer()
    payload = verify_capability(
        token_for(signer), public_key=signer.public_key, run_id=RUN, action=ROLLBACK
    )

    assert (payload.action, payload.target, payload.params) == (
        "rollback",
        "checkout",
        {"to_version": "v2.4.0"},
    )
    assert (payload.approval_id, payload.run_id, payload.safety_enabled) == ("apr-1", RUN, True)
    assert payload.expires_at - payload.issued_at == timedelta(seconds=60)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"target": "payment"}, "different target"),
        ({"action": "restart", "params": {}}, "different action"),
        ({"params": {"to_version": "v1.0.0"}}, "different params"),
        ({"approval_id": "apr-2"}, "different approval"),
        ({"proposal_digest": "e" * 64}, "different proposal digest"),
    ],
    ids=["target", "action", "params", "approval", "digest"],
)
def test_a_capability_cannot_be_moved_to_another_action(change: dict, reason: str) -> None:
    signer = issuer()
    other = ROLLBACK.model_copy(update=change)

    with pytest.raises(CapabilityError, match=reason):
        check(token_for(signer), signer.public_key, other)


def test_a_capability_from_another_run_is_refused() -> None:
    signer = CapabilityIssuer("run-other00001", "inc-1")
    with pytest.raises(CapabilityError, match="different run"):
        check(token_for(signer), signer.public_key)


# --- forged, broken, stale and reused tokens ----------------------------------------------------


def test_a_token_signed_with_another_key_is_refused() -> None:
    signer, attacker = issuer(), issuer()
    with pytest.raises(CapabilityError, match="signature is not valid"):
        check(token_for(attacker), signer.public_key)


def test_a_tampered_payload_breaks_the_signature() -> None:
    signer = issuer()
    body, signature = token_for(signer).split(".")
    other_body, _ = token_for(signer, ROLLBACK.model_copy(update={"target": "payment"})).split(".")

    with pytest.raises(CapabilityError, match="signature is not valid"):
        check(f"{other_body}.{signature}", signer.public_key)


@pytest.mark.parametrize("token", ["", "not-a-token", "a.b.c", "!!!.???"])
def test_malformed_or_missing_tokens_are_refused(token: str) -> None:
    with pytest.raises(CapabilityError):
        check(token, issuer().public_key)


def test_nothing_is_allowed_when_no_key_was_bound() -> None:
    signer = issuer()
    with pytest.raises(CapabilityError, match="no executor key"):
        verify_capability(token_for(signer), public_key=None, run_id=RUN, action=ROLLBACK)


def test_an_expired_capability_is_refused() -> None:
    signer = issuer()
    later = utcnow() + timedelta(seconds=61)
    with pytest.raises(CapabilityError, match="expired"):
        check(token_for(signer), signer.public_key, now=later)


def test_a_capability_issued_in_the_future_is_refused() -> None:
    """A token issued later than the verifier's clock (beyond a 5 s tolerance) is refused."""
    signer = issuer()
    with pytest.raises(CapabilityError, match="issued in the future"):
        check(token_for(signer), signer.public_key, now=utcnow() - timedelta(seconds=30))


def test_a_used_nonce_is_refused() -> None:
    signer = issuer()
    token = token_for(signer)
    payload = verify_capability(token, public_key=signer.public_key, run_id=RUN, action=ROLLBACK)

    with pytest.raises(CapabilityError, match="already used"):
        check(token, signer.public_key, used_nonces={payload.nonce})


def test_a_safety_off_capability_is_accepted_only_by_a_simulation() -> None:
    signer = issuer()
    token = token_for(signer, safety_enabled=False)

    check(token, signer.public_key, allow_safety_off=True)  # a simulated environment
    with pytest.raises(CapabilityError, match="only by a simulation"):
        check(token, signer.public_key)  # any real environment


# --- the simulator at its write boundary ------------------------------------------------------


async def bad_deployment(tmp_path: Path) -> SimulatedEnvironment:
    env = SimulatedEnvironment(
        run_id=RUN,
        scenarios_dir=str(REPO_ROOT / "fixtures" / "scenarios"),
        state_dir=str(tmp_path / "sandbox"),
    )
    await env.reset("bad-deployment", 11)
    return env


async def test_the_simulator_refuses_an_unsigned_write_and_changes_nothing(tmp_path: Path) -> None:
    env = await bad_deployment(tmp_path)
    issuer_for(env)  # a key is bound, but this write carries no capability
    before = env.store.load()

    outcome = await env.execute_authorized(ROLLBACK)

    after = env.store.load()
    assert outcome.ok is False and "not authorized" in outcome.message
    assert after["clock_minute"] == before["clock_minute"]  # the world did not move
    assert after["deployments"] == before["deployments"]
    assert after["action_log"] == []  # that log is for writes the gateway authorized
    [refused] = after["refused_writes"]  # the unauthorized attempt is audited separately
    assert refused["ok"] is False and "capability" not in refused
    assert after["refused_writes_total"] == 1


async def test_unauthorized_attempts_cannot_grow_the_world_state_without_limit(
    tmp_path: Path,
) -> None:
    """Refused callers are not authenticated: their audit trail is bounded and truncated."""
    env = await bad_deployment(tmp_path)
    issuer_for(env)
    huge = AuthorizedAction(action="restart", target="x" * 200_000, approval_id="a" * 50_000)

    for _ in range(60):
        await env.execute_authorized(huge)

    state = env.store.load()
    assert state["refused_writes_total"] == 60
    assert len(state["refused_writes"]) == 50  # only the most recent are kept
    entry = state["refused_writes"][-1]
    assert len(entry["target"]) <= 131 and len(entry["approval_id"]) <= 131
    assert len(json.dumps(state)) < 100_000  # 60 attempts of ~250 KB each left a small state


async def test_the_simulator_accepts_a_capability_once(tmp_path: Path) -> None:
    env = await bad_deployment(tmp_path)
    signed = sign(issuer_for(env), ROLLBACK)

    first = await env.execute_authorized(signed)
    replay = await env.execute_authorized(signed)

    assert first.ok and "rolled back checkout" in first.message
    assert replay.ok is False and "already used" in replay.message
    assert len(env.store.load()["used_nonces"]) == 1


async def test_a_reset_forgets_the_key_and_the_nonces(tmp_path: Path) -> None:
    """Each incident gets its own key: a capability from before a reset unlocks nothing."""
    env = await bad_deployment(tmp_path)
    signed = sign(issuer_for(env), ROLLBACK)
    await env.reset("bad-deployment", 11)

    outcome = await env.execute_authorized(signed)

    assert outcome.ok is False and "no executor key" in outcome.message
