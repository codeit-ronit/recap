"""The API with the Redis execution backend (M6 W3): runs are queued for workers.

The API (FastAPI's TestClient) and a worker (its own thread, event loop and Runtime, as a
separate process would have) share a temporary data folder and the in-repo fake Redis. Checked:
the typed contracts, the Idempotency-Key, backpressure (429), cancelling a queued run, the run's
statuses as its job moves, live SSE from the trace a worker stores (and resuming it), an API
restart while a run waits for a person, a Redis outage (503 for what needs the queue, reads
still served), and published runs staying read-only.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.dto import JobView, QueueView, RunDetail, RunStarted
from apps.api.main import create_app
from apps.worker.__main__ import admission
from recap.core.config import Settings
from recap.runtime import Runtime
from recap.workers.queue import JobQueue, QueueConfig
from recap.workers.worker import Worker, WorkerSettings
from tests.fake_redis import FakeRedisServer
from tests.helpers import OPERATOR_HEADERS

pytestmark = pytest.mark.e2e

BODY = {"scenario_id": "bad-deployment", "config": {"approval_mode": "manual"}}


@pytest.fixture
def server() -> Iterator[FakeRedisServer]:
    with FakeRedisServer.running() as running:
        yield running


def make_queue(server: FakeRedisServer, **overrides: Any) -> JobQueue:
    fields: dict[str, Any] = {
        "url": server.url,
        "heartbeat_seconds": 0.1,
        "heartbeat_ttl_seconds": 1.0,
    }
    return JobQueue(QueueConfig(**{**fields, **overrides}))


def api(runtime: Runtime, queue: JobQueue) -> TestClient:
    return TestClient(create_app(runtime, queue=queue), headers=OPERATOR_HEADERS)


@contextmanager
def worker_running(settings: Settings, server: FakeRedisServer) -> Iterator[None]:
    """A worker on its own thread and event loop, with its own Runtime (as its own process)."""
    ready = threading.Event()
    state: dict[str, Any] = {}

    async def main() -> None:
        state["loop"] = asyncio.get_running_loop()
        state["stop"] = asyncio.Event()
        worker = Worker(
            Runtime(settings),
            make_queue(server),
            WorkerSettings(block_seconds=0.1),
            admit=admission,  # as apps/worker runs it: every job checked again
        )
        ready.set()
        await worker.run(state["stop"])

    thread = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    thread.start()
    assert ready.wait(30)
    try:
        yield
    finally:
        state["loop"].call_soon_threadsafe(state["stop"].set)
        thread.join(60)
        assert not thread.is_alive()


def wait_for(predicate: Callable[[], Any], timeout: float = 90.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def status(client: TestClient, run_id: str) -> str:
    return str(client.get(f"/runs/{run_id}").json()["status"])


def pending_for(client: TestClient, run_id: str) -> dict[str, Any]:
    return wait_for(
        lambda: next(
            (p for p in client.get("/approvals/pending").json() if p["run_id"] == run_id), None
        )
    )


def decide(client: TestClient, request: dict[str, Any]) -> None:
    body = {"decision": "approved", "proposal_digest": request["proposal_digest"]}
    response = client.post(f"/approvals/{request['proposal_id']}", json=body)
    assert response.status_code == 200, response.text


def frames(text: str) -> list[int]:
    return [int(seq) for seq in re.findall(r"^id: (\d+)$", text, flags=re.MULTILINE)]


def test_a_run_is_queued_followed_live_and_finished_by_a_worker(
    settings: Settings, server: FakeRedisServer
) -> None:
    queue = make_queue(server)
    with api(Runtime(settings), queue) as client:
        response = client.post("/incidents/run", json=BODY, headers={"Idempotency-Key": "client-1"})
        assert response.status_code == 202, response.text
        started = RunStarted.model_validate(response.json())
        assert (started.status, started.queue_position, started.duplicate) == ("queued", 1, False)
        assert started.job_id and started.labels  # the labels a started run has always had
        again = client.post("/incidents/run", json=BODY, headers={"Idempotency-Key": "client-1"})
        assert again.status_code == 202 and again.json()["duplicate"] is True
        assert (again.json()["run_id"], again.json()["job_id"]) == (started.run_id, started.job_id)
        other = {**BODY, "scenario_id": "resource-pressure"}
        conflict = client.post(
            "/incidents/run", json=other, headers={"Idempotency-Key": "client-1"}
        )
        assert conflict.status_code == 422
        bad = client.post("/incidents/run", json=BODY, headers={"Idempotency-Key": "not a key!"})
        assert bad.status_code == 400

        detail = RunDetail.model_validate(client.get(f"/runs/{started.run_id}").json())
        assert detail.status == "queued" and detail.record is None
        assert detail.job is not None and detail.job.position == 1 and detail.job.status == "queued"
        view = QueueView.model_validate(client.get("/queue").json())
        assert (view.backend, view.reachable, view.depth, view.capacity) == ("redis", True, 1, 20)
        assert "password" not in (view.address or "")
        health = client.get("/health").json()
        assert (health["execution_backend"], health["queue_reachable"]) == ("redis", True)

        # Follow the run live from before a worker exists until its run.summary.
        streamed: dict[str, str] = {}

        def follow() -> None:
            with client.stream("GET", f"/runs/{started.run_id}/stream") as stream:
                streamed["text"] = "".join(stream.iter_text())

        follower = threading.Thread(target=follow)
        follower.start()
        with worker_running(settings, server):
            request = pending_for(client, started.run_id)
            assert status(client, started.run_id) == "running"
            running = client.get(f"/runs/{started.run_id}").json()["job"]
            assert running["status"] == "running" and running["worker_alive"] is True
            decide(client, request)
            wait_for(lambda: status(client, started.run_id) == "finished")
            follower.join(60)
        stored = client.get(f"/runs/{started.run_id}/events").json()
        seqs = [event["seq"] for event in stored]
        assert frames(streamed["text"]) == seqs  # every stored event once, in order, live
        assert stored[-1]["category"] == "run.summary"
        resumed = client.get(
            f"/runs/{started.run_id}/stream", headers={"Last-Event-ID": str(seqs[4])}
        )
        assert frames(resumed.text) == seqs[5:]  # resumes right after the last one received

        job = JobView.model_validate(client.get(f"/jobs/{started.job_id}").json())
        assert (job.status, job.category, job.runs) == ("completed", "RESOLVED", [started.run_id])
        summary = client.get(f"/runs/{started.run_id}").json()["summary"]
        assert summary["outcome"] == "resolved" and summary["interruption"] is None
        listed = client.get("/runs").json()
        assert [r["status"] for r in listed if r["run_id"] == started.run_id] == ["finished"]
        assert client.get("/jobs/job-nonexistent").status_code == 404


def test_a_full_queue_answers_429_and_a_queued_run_is_cancelled_before_it_starts(
    settings: Settings, server: FakeRedisServer
) -> None:
    with api(Runtime(settings), make_queue(server, capacity=1)) as client:
        first = client.post("/incidents/run", json=BODY)
        assert first.status_code == 202
        full = client.post("/incidents/run", json=BODY)
        assert full.status_code == 429 and full.headers["Retry-After"] == "5"
        run_id = first.json()["run_id"]
        cancel = client.post(f"/runs/{run_id}/cancel")
        assert cancel.status_code == 202 and cancel.json()["status"] == "cancelled"
        detail = client.get(f"/runs/{run_id}").json()
        assert detail["status"] == "cancelled" and detail["record"] is None  # nothing ran
        assert client.post(f"/runs/{run_id}/cancel").status_code == 409  # nothing left to stop
        assert client.post("/incidents/run", json=BODY).status_code == 202  # room again
        assert client.post("/runs/run-unknown/cancel").status_code == 404


def test_a_run_waiting_for_a_person_survives_an_api_restart(
    settings: Settings, server: FakeRedisServer
) -> None:
    with worker_running(settings, server):
        with api(Runtime(settings), make_queue(server)) as first_api:
            run_id = first_api.post("/incidents/run", json=BODY).json()["run_id"]
            pending_for(first_api, run_id)
        # The API process is gone; a new one, with nothing in memory, takes over.
        with api(Runtime(settings), make_queue(server)) as second_api:
            assert status(second_api, run_id) == "running"
            decide(second_api, pending_for(second_api, run_id))
            wait_for(lambda: status(second_api, run_id) == "finished")
            detail = second_api.get(f"/runs/{run_id}").json()
            assert detail["record"]["state"] == "RESOLVED"


def test_a_redis_outage_refuses_what_needs_the_queue_and_still_serves_reads(
    settings: Settings, server: FakeRedisServer
) -> None:
    with api(Runtime(settings), make_queue(server)) as client:
        server.stop()
        refused = client.post("/incidents/run", json=BODY)
        assert refused.status_code == 503 and "nothing was queued" in refused.json()["detail"]
        assert client.get("/runs").status_code == 200
        assert client.get("/health").json()["queue_reachable"] is False
        view = client.get("/queue").json()
        assert view["reachable"] is False and view["error"]
        assert client.post("/runs/run-anything/cancel").status_code == 503
        assert client.get("/jobs/job-anything").status_code == 503


def test_published_runs_stay_read_only_with_the_redis_backend(
    settings: Settings, server: FakeRedisServer
) -> None:
    with api(Runtime(settings), make_queue(server)) as client:
        published = client.get("/published").json()["runs"]
        assert published, "the repository publishes real-cluster runs (evals/live/MANIFEST.json)"
        run_id = published[0]["run_id"]
        detail = client.get(f"/runs/{run_id}").json()
        assert detail["status"] == "published" and detail["job"] is None
        assert client.post(f"/runs/{run_id}/cancel").status_code == 409
        assert client.get("/queue").json()["depth"] == 0  # nothing was queued for it


def test_without_redis_settings_the_api_runs_incidents_itself(settings: Settings) -> None:
    """The default backend: no Redis, nothing queued; the Idempotency-Key works per process."""
    with TestClient(create_app(Runtime(settings)), headers=OPERATOR_HEADERS) as client:
        assert client.get("/health").json()["execution_backend"] == "inprocess"
        body = {"scenario_id": "bad-deployment", "config": {"approval_mode": "auto"}}
        first = client.post("/incidents/run", json=body, headers={"Idempotency-Key": "k"})
        assert first.status_code == 202 and first.json()["status"] == "started"
        again = client.post("/incidents/run", json=body, headers={"Idempotency-Key": "k"})
        assert again.json()["run_id"] == first.json()["run_id"] and again.json()["duplicate"]
        view = QueueView.model_validate(client.get("/queue").json())
        assert view.backend == "inprocess" and view.workers == []
        wait_for(lambda: status(client, first.json()["run_id"]) == "finished")


def test_a_replay_runs_in_a_worker_exactly_as_it_would_in_process(
    settings: Settings, server: FakeRedisServer
) -> None:
    """A committed recording, replayed offline by a worker: the configuration the API checked
    reaches the worker with the same fields set (scenario overrides apply the same way)."""
    body = {
        "scenario_id": "bad-deployment",
        "config": {"provider": "replay", "replay_fixture": "qwen3-8b-bad-deployment"},
    }
    with api(Runtime(settings), make_queue(server)) as client, worker_running(settings, server):
        started = client.post("/incidents/run", json=body)
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        decide(client, pending_for(client, run_id))
        wait_for(lambda: status(client, run_id) == "finished")
        summary = client.get(f"/runs/{run_id}").json()["summary"]
    assert summary["provider_class"] == "replay"
    assert summary["model_requests"] == 0  # offline: answered from the recording, no model called
    assert summary["replay"]["fixture_id"] == "qwen3-8b-bad-deployment"
    assert summary["outcome"] == "resolved"
