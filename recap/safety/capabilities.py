"""Execution capabilities: signed, short-lived, single-use permission for ONE exact write.

Where it sits (audit finding 2.5):

    controller --reset--> environment.bind_executor(public key of this run)
    gateway: policy ALLOW + exact approval consumed --> CapabilityIssuer.issue(...)  -> token
    gateway --MCP tool call (args + token)--> MCP server --> environment.execute_authorized
    environment: verify_capability(token, bound key, action, used nonces) --> apply or refuse

Why: in V1 the MCP server and the environment trusted whoever called them, so any process
that could speak MCP (for example a second `python -m recap.tools.mcp_server` for an
existing run) could roll back, restart or delete with an empty approval ID. The gateway's
checks only protected callers that went through the gateway. Now the final privilege
barrier is at the write boundary itself, and it holds even if the MCP caller is compromised.

How it works, in simple words:

* Each run makes a fresh Ed25519 key pair. The PRIVATE key stays in the controller's memory
  and is never written anywhere. Only the PUBLIC key is handed to the environment.
* For every write the gateway allowed, it signs a small payload that names exactly what may
  happen: run, incident, action, target, parameters, approval, a random nonce and a 60 s
  expiry. The token is `base64url(payload JSON) + "." + base64url(signature)`.
* The environment checks the signature with the public key it was given, checks that the
  payload names exactly the action it was asked to do, that it has not expired, and that
  its nonce was never used before, and records the nonce in the same transaction as the
  change. A forged, altered, expired, replayed or misdirected token is refused, and
  nothing in the world changes.

Asymmetric keys mean the environment needs no secret: a process that can read the public
key (from the sandbox state file, for example) still cannot sign anything.

Limits, honestly: whoever controls the controller process (or can rewrite the sandbox state
file and so bind a key of its own) can still act; the state files are trusted state (ADR
0002). A capability proves "RECAP's gateway allowed this exact action", not "a human was
right to approve it".
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import ValidationError

from recap.core.errors import CapabilityError
from recap.core.ids import canonical_json, utcnow
from recap.core.models import Contract
from recap.core.observations import AuthorizedAction, EnvironmentAction
from recap.core.signals import as_utc

CAPABILITY_VERSION = 1
DEFAULT_TTL_SECONDS = 60.0  # the gateway sends the call right away; a minute is plenty


class CapabilityPayload(Contract):
    """What one capability allows: exactly one action, on one target, once, for a short time."""

    version: Literal[1] = CAPABILITY_VERSION
    run_id: str
    incident_id: str
    action: EnvironmentAction
    target: str
    params: dict[str, Any]
    approval_id: str  # "" only in the simulation-only safety-off ablation
    proposal_digest: str
    safety_enabled: bool  # False only in the simulation-only ablation (see verify_capability)
    nonce: str  # random; the environment refuses a nonce it has seen before
    issued_at: datetime
    expires_at: datetime


class CapabilityIssuer:
    """Signs capabilities for one run. Lives in the controller; its private key never leaves."""

    def __init__(
        self, run_id: str, incident_id: str, ttl_seconds: float = DEFAULT_TTL_SECONDS
    ) -> None:
        self.run_id = run_id
        self.incident_id = incident_id
        self.ttl_seconds = ttl_seconds
        self._private_key = Ed25519PrivateKey.generate()  # fresh per run, memory only
        raw = self._private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_key = _b64encode(raw)  # what the environment is told to trust

    def issue(
        self,
        *,
        action: EnvironmentAction,
        target: str,
        params: Mapping[str, Any],
        approval_id: str,
        proposal_digest: str,
        safety_enabled: bool = True,
    ) -> str:
        """A token allowing exactly this action once, for the next `ttl_seconds`."""
        now = utcnow()
        payload = CapabilityPayload(
            run_id=self.run_id,
            incident_id=self.incident_id,
            action=action,
            target=target,
            params=dict(params),
            approval_id=approval_id,
            proposal_digest=proposal_digest,
            safety_enabled=safety_enabled,
            nonce=secrets.token_urlsafe(16),
            issued_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
        )
        body = canonical_json(payload.model_dump(mode="json")).encode("utf-8")
        signature = self._private_key.sign(body)
        return f"{_b64encode(body)}.{_b64encode(signature)}"


def verify_capability(
    token: str,
    *,
    public_key: str | None,
    run_id: str,
    action: AuthorizedAction,
    used_nonces: Collection[str] = (),
    allow_safety_off: bool = False,
    now: datetime | None = None,
) -> CapabilityPayload:
    """Check that `token` allows exactly `action` in this run right now; raise if it does not.

    `public_key` is the key the controller bound with `bind_executor` (None: nothing bound,
    so nothing is allowed). `used_nonces` are the nonces this environment already accepted.
    `allow_safety_off` may be True only for a simulated environment: the safety-off
    ablation exists to show what would happen inside a sandbox, never on real systems.
    The caller must record the returned payload's nonce in the same transaction as the change.
    """
    if not public_key:
        raise CapabilityError("no executor key is bound to this environment")
    if not token:
        raise CapabilityError("the call carries no capability")
    payload = _verified_payload(token, public_key)
    expected = {
        "run": (payload.run_id, run_id),
        "action": (payload.action, action.action),
        "target": (payload.target, action.target),
        "params": (canonical_json(payload.params), canonical_json(action.params)),
        "approval": (payload.approval_id, action.approval_id),
        "proposal digest": (payload.proposal_digest, action.proposal_digest),
    }
    for what, (allowed, asked) in expected.items():
        if allowed != asked:
            raise CapabilityError(f"the capability is for a different {what}")
    moment = as_utc(now or utcnow())
    if moment > as_utc(payload.expires_at):
        raise CapabilityError("the capability has expired")
    if as_utc(payload.issued_at) > moment + timedelta(seconds=5):  # small clock tolerance
        raise CapabilityError("the capability was issued in the future")
    if payload.nonce in used_nonces:
        raise CapabilityError("the capability was already used")
    if not payload.safety_enabled and not allow_safety_off:
        raise CapabilityError("a safety-off capability is accepted only by a simulation")
    return payload


def _verified_payload(token: str, public_key: str) -> CapabilityPayload:
    """Decode the token and check its signature BEFORE trusting a single field of it."""
    try:
        body_part, signature_part = token.split(".")
        body, signature = _b64decode(body_part), _b64decode(signature_part)
        key = Ed25519PublicKey.from_public_bytes(_b64decode(public_key))
    except (ValueError, binascii.Error) as exc:
        raise CapabilityError("the capability is malformed") from exc
    try:
        key.verify(signature, body)
    except InvalidSignature as exc:
        raise CapabilityError("the capability's signature is not valid") from exc
    try:
        return CapabilityPayload.model_validate(json.loads(body))
    except (ValueError, ValidationError) as exc:
        raise CapabilityError("the capability's payload is malformed") from exc


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
