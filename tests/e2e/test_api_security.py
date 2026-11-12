"""The API's security controls, signed in and not (audit finding 2.4; see apps/api/auth.py).

The audit reproductions in test_audit_api_security.py show that anonymous callers are now
refused. These tests cover the rest: a wrong token, the identity a decision records, a live
approval's typed name (whichever server on the run's data folder is asked), the origin and
host checks, the decision log endpoint, cancellation, and the token file.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import OperatorAuth, allowed_hosts_from_environment, origin_allowed
from apps.api.main import create_app
from recap.core.config import Settings
from recap.core.models import ApprovalRequest, ApprovalStatus, MitigationProposal
from recap.environments.kubernetes import KubernetesEnvironment
from recap.environments.kubernetes.preflight import PreflightError
from recap.runtime import Runtime
from tests.e2e.test_kind_pipeline import adapter, kind_runtime
from tests.helpers import OPERATOR_HEADERS
from tests.unit.test_kind_engine import WRITE_OPS, kind_options

WRITE_ENDPOINTS = [
    ("/incidents/run", {"scenario_id": "bad-deployment"}),
    ("/approvals/prop-x", {"decision": "approved", "proposal_digest": "0" * 64}),
    ("/evals/run", {}),
    ("/runs/run-x/cancel", None),
    ("/environment/check", None),
]


def open_request(runtime: Runtime, proposal_id: str = "prop-sec-001") -> ApprovalRequest:
    proposal = MitigationProposal(
        proposal_id=proposal_id,
        incident_id="inc-sec-001",
        action="execute_rollback",
        target="checkout",
        params={"to_version": "v2.4.0"},
        rationale="errors started right after v2.4.1",
        evidence_ids=["ev-1-001"],
    )
    return runtime.approvals.create_request(proposal, run_id="run-sec-001", ttl_seconds=900)


def wait_for(predicate, timeout: float = 60.0):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


@pytest.mark.parametrize(("path", "body"), WRITE_ENDPOINTS, ids=[p for p, _ in WRITE_ENDPOINTS])
def test_a_wrong_token_is_refused_on_every_state_changing_endpoint(
    settings: Settings, path: str, body: dict | None
) -> None:
    with TestClient(create_app(Runtime(settings))) as client:
        response = client.post(path, json=body, headers={"Authorization": "Bearer not-the-token"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_the_session_endpoint_names_the_operator(settings: Settings) -> None:
    with TestClient(create_app(Runtime(settings))) as client:
        anonymous = client.get("/auth/session")
        signed_in = client.get("/auth/session", headers=OPERATOR_HEADERS)

    assert anonymous.status_code == 401
    assert signed_in.json() == {"operator": "local-operator", "actor": "operator:local-operator"}


def test_a_decision_records_the_authenticated_operator(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECAP_OPERATOR_NAME", "jamie")
    runtime = Runtime(settings)
    request = open_request(runtime)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        body = {"decision": "approved", "proposal_digest": request.proposal_digest}
        decided = client.post(f"/approvals/{request.proposal_id}", json=body)
        log = client.get(f"/approvals/{request.proposal_id}/events").json()
        recent = client.get("/approvals/events", params={"limit": 1}).json()

    assert decided.status_code == 200 and decided.json()["actor"] == "operator:jamie"
    assert [(e["kind"], e["actor"]) for e in log] == [
        ("requested", "recap"),
        ("approved", "operator:jamie"),
    ]
    assert recent[0]["kind"] == "approved"


def test_a_body_that_names_its_own_actor_is_rejected(settings: Settings) -> None:
    runtime = Runtime(settings)
    request = open_request(runtime)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        body = {
            "decision": "approved",
            "proposal_digest": request.proposal_digest,
            "actor": "incident-commander",
        }
        response = client.post(f"/approvals/{request.proposal_id}", json=body)

    assert response.status_code == 422  # an unknown field is an error, not silently ignored
    assert runtime.approvals.get_request(request.proposal_id).status == ApprovalStatus.PENDING


def test_a_rejection_through_the_api_revokes_an_unused_approval(settings: Settings) -> None:
    runtime = Runtime(settings)
    request = open_request(runtime)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        url = f"/approvals/{request.proposal_id}"
        digest = request.proposal_digest
        client.post(url, json={"decision": "approved", "proposal_digest": digest})
        revoked = client.post(url, json={"decision": "rejected", "proposal_digest": digest})
        again = client.post(url, json={"decision": "approved", "proposal_digest": digest})

    assert revoked.status_code == 200 and revoked.json()["decision"] == "rejected"
    assert again.status_code == 409 and "not pending" in again.json()["detail"]


def live(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    """This runtime's target is a real cluster (only what the approval endpoint asks)."""
    info = runtime.environment_info().model_copy(update={"is_simulated": False, "kind": "kind"})
    monkeypatch.setattr(runtime, "environment_info", lambda: info)


def test_a_live_approval_needs_the_target_services_exact_name(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5: on a real cluster the dashboard asks what the console asks: the target service's
    exact name. One click, or a wrong name, decides nothing."""
    runtime = Runtime(settings)
    live(runtime, monkeypatch)
    request = open_request(runtime)
    url = f"/approvals/{request.proposal_id}"
    digest = request.proposal_digest
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        bare = client.post(url, json={"decision": "approved", "proposal_digest": digest})
        wrong = client.post(
            url,
            json={"decision": "approved", "proposal_digest": digest, "confirm_target": "check"},
        )
        pending = runtime.approvals.get_request(request.proposal_id).status
        named = client.post(
            url,
            json={"decision": "approved", "proposal_digest": digest, "confirm_target": "checkout"},
        )
        log = client.get(f"{url}/events").json()

    assert bare.status_code == wrong.status_code == 400
    assert "type the target service's exact name" in bare.json()["detail"]
    assert pending == ApprovalStatus.PENDING  # nothing was decided by either
    assert named.status_code == 200 and named.json()["decision"] == "approved"
    assert [e["kind"] for e in log] == ["requested", "refused", "refused", "approved"]
    assert "without the target service's exact name" in log[1]["detail"]  # each attempt kept
    assert log[-1]["detail"].startswith("dashboard or API; typed the target service's name;")


def test_a_live_rejection_needs_no_name(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(settings)
    live(runtime, monkeypatch)
    request = open_request(runtime)
    body = {"decision": "rejected", "proposal_digest": request.proposal_digest}
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        rejected = client.post(f"/approvals/{request.proposal_id}", json=body)

    assert rejected.status_code == 200 and rejected.json()["decision"] == "rejected"
    note = runtime.approvals.events(request.proposal_id)[-1].detail
    assert note.startswith("dashboard or API; answered") and "typed" not in note


@pytest.mark.e2e
def test_a_live_runs_approval_needs_the_name_on_a_server_configured_for_the_simulator(
    settings: Settings, tmp_path: Path
) -> None:
    """Two API servers on one data folder: one whose target is the Kind demo (the offline
    fake cluster), one configured for the simulator. The approval store is the folder's, so
    both list the live run's request. The simulator's server decides it by the run's own
    target: one click, or a wrong name, decides nothing (400); the exact name approves, the
    decision log says it was typed, and the rollback then reaches the cluster under that
    approval."""
    kind, _ = kind_runtime(settings, tmp_path)
    simulated = Runtime(settings)  # the same data folder, configured for the simulator
    assert kind.settings.paths.recap_db == simulated.settings.paths.recap_db
    assert simulated.environment_info().is_simulated and not kind.environment_info().is_simulated
    with (
        TestClient(create_app(kind), headers=OPERATOR_HEADERS) as live_server,
        TestClient(create_app(simulated), headers=OPERATOR_HEADERS) as other_server,
    ):
        run_id = live_server.post(
            "/incidents/run",
            json={"scenario_id": "kind-bad-release", "config": {"memory_write": False}},
        ).json()["run_id"]
        [request] = wait_for(lambda: other_server.get("/approvals/pending").json())
        url = f"/approvals/{request['proposal_id']}"
        approve = {"decision": "approved", "proposal_digest": request["proposal_digest"]}
        bare = other_server.post(url, json=approve)
        wrong = other_server.post(url, json={**approve, "confirm_target": "Backend"})
        status = simulated.approvals.get_request(request["proposal_id"]).status
        named = other_server.post(url, json={**approve, "confirm_target": "backend"})

        def finished() -> dict | None:
            body = live_server.get(f"/runs/{run_id}").json()
            return body if body["status"] == "finished" else None

        record = wait_for(finished)["record"]

    assert request["run_id"] == run_id and request["proposal"]["target"] == "backend"
    assert bare.status_code == wrong.status_code == 400
    assert "type the target service's exact name" in bare.json()["detail"]
    assert status == ApprovalStatus.PENDING  # neither decided anything
    assert named.status_code == 200 and named.json()["decision"] == "approved"
    log = simulated.approvals.events(request["proposal_id"])
    assert [e.kind for e in log] == ["requested", "refused", "refused", "approved", "consumed"]
    assert log[3].detail.startswith("dashboard or API; typed the target service's name;")
    assert record["state"] == "RESOLVED", record["outcome_note"]
    [approval] = record["approvals"]
    [write] = adapter(kind, run_id).state.writes()
    assert (write["outcome"], write["approval_id"]) == ("executed", approval["approval_id"])


def test_the_environment_check_reaches_the_target_and_changes_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """POST /environment/check: a real cluster's adapter runs its own preflight, the way a
    reset first reaches the cluster; nothing is written and no run starts."""
    runtime = Runtime(settings)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        simulated = client.post("/environment/check").json()
    assert simulated["ok"] and simulated["simulated"]
    assert simulated["checks"][0]["name"] == "simulated environment"

    kind = KubernetesEnvironment("readiness-check", **kind_options(tmp_path))
    monkeypatch.setattr(runtime, "environment_factory", lambda config, run_id: kind)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        reached = client.post("/environment/check").json()

        async def refused() -> None:
            raise PreflightError("the cluster's CA differs from the recorded fingerprint")

        monkeypatch.setattr(kind, "preflight", refused)
        failed = client.post("/environment/check").json()

    assert reached["ok"] and not reached["simulated"] and reached["environment_kind"] == "kind"
    assert not [c for c in kind.state.commands() if c["op"] in WRITE_OPS]  # nothing changed
    assert not failed["ok"] and "recorded fingerprint" in failed["checks"][0]["detail"]


def test_a_write_from_a_page_on_another_origin_is_refused(settings: Settings) -> None:
    with TestClient(create_app(Runtime(settings)), headers=OPERATOR_HEADERS) as client:
        foreign = client.post(
            "/incidents/run",
            json={"scenario_id": "nope"},
            headers={"Origin": "https://evil.example"},
        )
        dev_server = client.post(
            "/incidents/run",
            json={"scenario_id": "nope"},
            headers={"Origin": "http://localhost:5173"},
        )
        read = client.get("/health", headers={"Origin": "https://evil.example"})

    assert foreign.status_code == 403
    assert dev_server.status_code == 404  # allowed through to the endpoint (unknown scenario)
    assert read.status_code == 200  # reads are not state-changing


def test_a_replay_or_a_provider_choice_still_needs_the_operator(settings: Settings) -> None:
    """The per-run choices added in M4 go through the same doors as every other write."""
    replay = {
        "scenario_id": "m3-inventory-leak-behind-frontend",
        "config": {"provider": "replay", "replay_fixture": "qwen3-8b-unfamiliar-parallel"},
    }
    model = {"scenario_id": "bad-deployment", "config": {"provider": "ollama"}}
    with TestClient(create_app(Runtime(settings))) as client:
        anonymous = [client.post("/incidents/run", json=body) for body in (replay, model)]
        foreign = client.post(
            "/incidents/run",
            json=replay,
            headers={**OPERATOR_HEADERS, "Origin": "https://evil.example"},
        )
        rebound = client.get("/runs/run-x/timeline", headers={"Host": "evil.example"})
        started = dict(client.app.state.manager.tasks)  # type: ignore[attr-defined]

    assert [r.status_code for r in anonymous] == [401, 401]
    assert foreign.status_code == 403 and rebound.status_code == 400
    assert started == {}  # nothing ran


def test_without_configuration_only_loopback_host_names_are_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RECAP_ALLOWED_HOSTS", raising=False)
    assert allowed_hosts_from_environment() == ["localhost", "127.0.0.1", "[::1]"]


def test_the_origin_check_accepts_only_allowed_host_names() -> None:
    assert origin_allowed(None, ["localhost"])  # not a browser: no Origin header
    assert origin_allowed("http://[::1]:5173", ["[::1]"])
    assert not origin_allowed("http://localhost.evil.example", ["localhost"])
    assert not origin_allowed("file://localhost", ["localhost"])


def test_the_ipv6_loopback_host_is_allowed(settings: Settings) -> None:
    app = create_app(Runtime(settings), allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    with TestClient(app, base_url="http://[::1]:8000") as client:
        assert client.get("/health").status_code == 200
    with TestClient(app, base_url="http://evil.example") as client:
        assert client.get("/health").status_code == 400


@pytest.mark.e2e
def test_cancelling_a_run_that_waits_for_approval_expires_its_request(settings: Settings) -> None:
    runtime = Runtime(settings)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        run_id = client.post(
            "/incidents/run",
            json={"scenario_id": "bad-deployment", "config": {"approval_mode": "manual"}},
        ).json()["run_id"]
        pending = wait_for(lambda: client.get("/approvals/pending").json())[0]

        cancelled = client.post(f"/runs/{run_id}/cancel")
        record = wait_for(
            lambda: (body := client.get(f"/runs/{run_id}").json())["status"] != "running" and body
        )["record"]
        late = client.post(
            f"/approvals/{pending['proposal_id']}",
            json={"decision": "approved", "proposal_digest": pending["proposal_digest"]},
        )
        unknown = client.post("/runs/run-does-not-exist/cancel")

    assert cancelled.status_code == 202
    assert record["state"] == "FAILED" and record["outcome_note"] == "run cancelled"
    assert late.status_code == 409  # nobody can approve the cancelled run's proposal
    kinds = [e.kind for e in runtime.approvals.events(pending["proposal_id"])]
    assert kinds[:2] == ["requested", "expired"] and kinds[-1] == "refused"
    assert unknown.status_code == 404


def test_a_generated_token_is_saved_for_this_user_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RECAP_OPERATOR_TOKEN", raising=False)

    first = OperatorAuth.from_environment(tmp_path)
    second = OperatorAuth.from_environment(tmp_path)  # the next start reads the same file

    token_file = tmp_path / "operator-token"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    token = token_file.read_text().strip()
    assert len(token) >= 40
    for auth in (first, second):
        assert auth._token == token.encode()


def test_a_token_file_others_can_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RECAP_OPERATOR_TOKEN", raising=False)
    token_file = tmp_path / "operator-token"
    token_file.write_text("planted-token-0123456789\n")
    token_file.chmod(0o644)  # readable by everyone

    with pytest.raises(RuntimeError, match="readable by this user only"):
        OperatorAuth.from_environment(tmp_path)

    token_file.chmod(0o600)  # the owner's private file is accepted
    assert OperatorAuth.from_environment(tmp_path)._token == b"planted-token-0123456789"


def test_a_symlinked_token_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECAP_OPERATOR_TOKEN", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("someone-elses-token-0123456789\n")
    elsewhere.chmod(0o600)
    (tmp_path / "operator-token").symlink_to(elsewhere)

    with pytest.raises(RuntimeError, match="cannot be read safely"):
        OperatorAuth.from_environment(tmp_path)


def test_reads_are_open_on_loopback_and_every_write_still_needs_the_token(
    settings: Settings,
) -> None:
    """The threat model's stance: on a loopback host name the API's reads need no token, so the
    dashboard can show runs, their evidence, the pending approvals and the published runs, while
    every state-changing request without the operator token is refused (M6 W6: the W6 mapping
    found only /health and /providers asserted readable)."""
    from tests.unit.test_api_metrics import run_with_approval

    runtime = Runtime(settings)
    app = create_app(runtime)
    with TestClient(app, headers=OPERATOR_HEADERS) as operator:
        run_id = run_with_approval(operator, "bad-deployment")["record"]["run_id"]
    open_request(runtime)  # a pending approval, as the dashboard lists it
    with TestClient(app) as anonymous:  # no Authorization header at all
        reads = ["/runs", f"/runs/{run_id}", f"/runs/{run_id}/timeline", f"/runs/{run_id}/evidence"]
        for path in [*reads, "/approvals/pending", "/published", "/queue"]:
            assert anonymous.get(path).status_code == 200, path
        assert run_id in anonymous.get("/runs").text
        pending = anonymous.get("/approvals/pending").json()
        assert [request["proposal_id"] for request in pending] == ["prop-sec-001"]
        for path, body in WRITE_ENDPOINTS:
            assert anonymous.post(path, json=body).status_code == 401, path
