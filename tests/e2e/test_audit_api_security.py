"""Audit 2.4 (part 2): an anonymous HTTP client must not approve actions or start work.

What the audit found
--------------------
apps/api/main.py has no authentication at all:
  * POST /approvals/{proposal_id} records a decision for anyone who sends the proposal's
    digest, and GET /approvals/pending hands that digest out to anyone;
  * the approver "identity" is the free-text body field `actor`, so a client can claim to
    be anybody, and that claim is written into the audit record;
  * there is no trusted-host check (only CORS is configured, and CORS does not stop this):
    a web page the operator visits can use DNS rebinding to reach the local API under its
    own host name, e.g. "evil.example";
  * POST /incidents/run and POST /evals/run start background work for anyone.

How these tests reproduce it
----------------------------
They call the real FastAPI app through fastapi.testclient.TestClient with a temporary data
directory, and they send NO credentials. For the approval tests a pending request is put
straight into the approval store the API reads, so no simulated run is needed (and the
setup does not depend on the run endpoint, which must itself start refusing anonymous
callers). The tests assert only on HTTP status codes and on what ends up stored, never on
how the future login works (a token, a session cookie, ...), so the fix is free to choose.

Every test is marked xfail(strict=True) until the fix lands in M1: each fails today for
the reason above, and strict=True makes pytest report an error as soon as it passes, so
the marker is removed together with the fix.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api import service
from apps.api.main import create_app
from recap.core.config import Settings
from recap.core.models import ApprovalRequest, ApprovalStatus, MitigationProposal
from recap.runtime import Runtime

FORGED_ACTOR = "incident-commander"  # a name the client simply typed; nothing proves it


# --- helpers ----------------------------------------------------------------------------------


def open_request(runtime: Runtime) -> ApprovalRequest:
    """Put one pending approval request (a rollback) in the store that the API reads."""
    proposal = MitigationProposal(
        proposal_id="prop-api-001",
        incident_id="inc-api-001",
        action="execute_rollback",
        target="checkout",
        params={"to_version": "v2.4.0"},
        rationale="errors started right after v2.4.1",
        evidence_ids=["ev-1-001"],
    )
    return runtime.approvals.create_request(proposal, run_id="run-api-001", ttl_seconds=900)


def approve_anonymously(
    client: TestClient, request: ApprovalRequest, **extra: str
) -> httpx.Response:
    """POST /approvals/{id} the way the dashboard does today: correct digest, no credentials."""
    body = {"decision": "approved", "proposal_digest": request.proposal_digest, **extra}
    return client.post(f"/approvals/{request.proposal_id}", json=body)


# --- 4. approving needs credentials ---------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.4: POST /approvals accepts decisions without credentials; fixed in M1",
)
def test_an_approval_without_credentials_is_refused(settings: Settings) -> None:
    """POST /approvals/{id} with no credentials is refused and records nothing."""
    runtime = Runtime(settings)
    request = open_request(runtime)
    with TestClient(create_app(runtime)) as client:
        response = approve_anonymously(client, request)

    assert response.status_code in {401, 403}, response.text
    # Nothing was decided: the request still waits and no decision is stored.
    stored = runtime.approvals.get_request(request.proposal_id)
    assert stored is not None and stored.status == ApprovalStatus.PENDING
    assert runtime.approvals.approvals_for(request.incident_id) == []


# --- 5. the approver identity is not whatever the client typed ------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.4: the body field 'actor' is stored as the approver identity; fixed in M1",
)
def test_the_actor_field_in_the_body_never_becomes_the_approver_identity(
    settings: Settings,
) -> None:
    """Whatever the response, no stored decision may name the client-chosen 'actor'."""
    runtime = Runtime(settings)
    request = open_request(runtime)
    with TestClient(create_app(runtime)) as client:
        approve_anonymously(client, request, actor=FORGED_ACTOR)

    actors = [approval.actor for approval in runtime.approvals.approvals_for(request.incident_id)]
    assert FORGED_ACTOR not in actors


# --- 6. only our own host name is served ------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.4: no trusted-host check, so DNS rebinding reaches the API; fixed in M1",
)
def test_a_request_for_a_foreign_host_name_is_refused(settings: Settings) -> None:
    """A request whose Host header names another site (DNS rebinding) is refused."""
    with TestClient(create_app(Runtime(settings))) as client:
        # /health needs no other permission, so a refusal here can only come from the host check.
        response = client.get("/health", headers={"Host": "evil.example"})

    assert response.status_code in {400, 403}, response.text


# --- 7. starting work needs credentials -------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.4: POST /incidents/run starts runs without credentials; fixed in M1",
)
def test_starting_a_run_without_credentials_is_refused(settings: Settings) -> None:
    """POST /incidents/run with no credentials is refused and starts no run."""
    with TestClient(create_app(Runtime(settings))) as client:
        response = client.post(
            "/incidents/run",
            json={"scenario_id": "bad-deployment", "config": {"approval_mode": "manual"}},
        )
        # The server keeps one background task per run it started.
        started = dict(client.app.state.manager.tasks)  # type: ignore[attr-defined]

    assert response.status_code in {401, 403}, response.text
    assert started == {}


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.4: POST /evals/run starts evaluations without credentials; fixed in M1",
)
def test_starting_an_evaluation_without_credentials_is_refused(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /evals/run with no credentials is refused and starts no evaluation."""

    async def no_evaluation(*args: object, **kwargs: object) -> None:
        """Stands in for the real evaluation, which would run every scenario."""

    # If the request is (wrongly) accepted, nothing heavy runs and nothing is written to evals/.
    monkeypatch.setattr(service, "run_evaluation", no_evaluation)
    monkeypatch.setattr(service, "new_report_dir", lambda: tmp_path / "eval")
    with TestClient(create_app(Runtime(settings))) as client:
        response = client.post("/evals/run", json={})
        started = client.app.state.manager.eval_task  # type: ignore[attr-defined]

    assert response.status_code in {401, 403}, response.text
    assert started is None
