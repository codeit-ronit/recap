"""The job queue and a worker against a REAL Redis server (M6 W3).

Skipped unless RECAP_TEST_REDIS_URL names one (Redis 6.2 or newer, for BLMOVE). Use a
throwaway server, never one that holds real jobs: one test drops every client connection
(CLIENT KILL), as a Redis restart would. For example:

    docker run --rm -d --name recap-test-redis -p 127.0.0.1:6390:6379 redis:7.4.2-alpine
    export RECAP_TEST_REDIS_URL=redis://127.0.0.1:6390/15
    .venv/bin/pytest tests/integration/test_workers_redis.py

Every key is written under a fresh prefix, and deleted afterwards. The rest of the W3 tests use
tests/fake_redis.py, which implements the same commands; these check that the real server
agrees with it, including when it drops connections.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections.abc import Iterator

import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState
from recap.runtime import Runtime
from recap.workers.queue import JobQueue, QueueConfig, QueueFull
from recap.workers.resp import Redis, RedisError, RedisUnavailable
from recap.workers.worker import Worker, WorkerSettings

URL = os.environ.get("RECAP_TEST_REDIS_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="RECAP_TEST_REDIS_URL is not set (no real Redis)")


@pytest.fixture
def queue() -> Iterator[JobQueue]:
    prefix = f"recap-test-{uuid.uuid4().hex[:8]}"
    config = QueueConfig(
        url=URL, prefix=prefix, capacity=3, heartbeat_seconds=0.2, heartbeat_ttl_seconds=1.0
    )
    made = JobQueue(config)
    try:
        yield made
    finally:
        redis = Redis(URL)
        keys = redis.execute("KEYS", f"{prefix}:*") or []
        if keys:
            redis.delete(*keys)


def drop_every_connection() -> int:
    """What a Redis restart does to its clients: every other connection is closed."""
    return int(Redis(URL).execute("CLIENT", "KILL", "TYPE", "normal", "SKIPME", "yes"))


def test_a_real_redis_agrees_with_the_queue_s_semantics(queue: JobQueue) -> None:
    job, created = queue.submit(
        run_id="run-a", scenario_id="bad-deployment", config={}, idempotency_key="key-a"
    )
    again, created_again = queue.submit(
        run_id="run-b", scenario_id="bad-deployment", config={}, idempotency_key="key-a"
    )
    assert created and not created_again and again.job_id == job.job_id
    queue.submit(run_id="run-c", scenario_id="bad-deployment", config={})
    queue.submit(run_id="run-d", scenario_id="bad-deployment", config={})
    with pytest.raises(QueueFull):
        queue.submit(run_id="run-e", scenario_id="bad-deployment", config={})
    claimed = queue.claim("w", block_seconds=0.2)
    assert claimed is not None and claimed.job_id == job.job_id and claimed.attempt == 1
    assert queue.held_by("w") == [job.job_id]
    queue.register("w", {"pid": 1})
    assert queue.heartbeat("w", {"pid": 1})
    queue.finish(claimed, "w", status="completed", category="RESOLVED")
    assert queue.held_by("w") == [] and queue.snapshot()["counters"]["completed"] == 1


def test_a_transaction_error_on_the_real_server_is_raised(queue: JobQueue) -> None:
    redis = Redis(URL)
    key = queue.queue_key + ":text"
    redis.set(key, "plain")
    with pytest.raises(RedisError):  # WRONGTYPE inside EXEC: raised, as with the fake
        redis.transaction([("HSET", key, "field", "value")])


def test_a_claim_on_the_real_server_wakes_when_another_client_submits(queue: JobQueue) -> None:
    got: dict[str, object] = {}

    def claim() -> None:
        started = time.monotonic()
        got["job"] = queue.claim("w", block_seconds=5, redis=queue.connection())
        got["waited"] = time.monotonic() - started

    claimer = threading.Thread(target=claim)
    claimer.start()
    time.sleep(0.3)  # the claim is blocked in BLMOVE on its own connection
    job, _ = queue.submit(run_id="run-wake", scenario_id="bad-deployment", config={})
    claimer.join(10)
    claimed = got.get("job")
    assert claimed is not None and claimed.job_id == job.job_id  # type: ignore[attr-defined]
    assert 0.25 < float(got["waited"]) < 3  # type: ignore[arg-type]


def test_a_heartbeat_expires_on_the_real_server_and_cannot_be_refreshed(queue: JobQueue) -> None:
    queue.register("w", {"pid": 1})
    assert queue.alive("w") and queue.workers() == {"w": {"pid": 1}}
    time.sleep(queue.config.heartbeat_ttl_seconds + 0.3)  # Redis expires the key itself
    assert queue.workers() == {"w": None} and not queue.alive("w")
    assert not queue.heartbeat("w", {"pid": 1})  # fencing: SET XX finds no key


def test_dropped_connections_are_an_outage_and_the_client_reconnects(queue: JobQueue) -> None:
    job, _ = queue.submit(run_id="run-drop", scenario_id="bad-deployment", config={})
    assert queue.get(job.job_id) is not None  # the queue's connection is open
    assert drop_every_connection() >= 1
    with pytest.raises(RedisUnavailable):
        queue.get(job.job_id)  # the dropped connection: an outage, never a wrong answer
    found = queue.get(job.job_id)  # the next command opens a new connection
    assert found is not None and found.status == "queued" and queue.depth() == 1


@pytest.mark.e2e
async def test_a_worker_runs_an_incident_with_a_real_redis(
    settings: Settings, queue: JobQueue
) -> None:
    runtime = Runtime(settings)
    job, _ = queue.submit(
        run_id="run-real-redis", scenario_id="bad-deployment", config={"approval_mode": "auto"}
    )
    stop = asyncio.Event()
    worker = Worker(runtime, queue, WorkerSettings(block_seconds=0.2))
    task = asyncio.create_task(worker.run(stop))
    try:
        for _ in range(1800):
            found = queue.get(job.job_id)
            if found is not None and found.terminal:
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=60)
    done = queue.get(job.job_id)
    assert done is not None and (done.status, done.category) == ("completed", "RESOLVED")
    record = runtime.runs.get(job.run_id)
    assert record is not None and record.state == IncidentState.RESOLVED


@pytest.mark.e2e
async def test_a_worker_survives_redis_dropping_its_connections_mid_run(
    settings: Settings, queue: JobQueue
) -> None:
    """Every connection dropped while the run waits for a person (as a Redis restart would):
    the worker reconnects for its heartbeats and claims, and the job still ends recorded."""
    runtime = Runtime(settings)
    job, _ = queue.submit(
        run_id="run-real-drop", scenario_id="bad-deployment", config={"approval_mode": "manual"}
    )
    stop = asyncio.Event()
    worker = Worker(runtime, queue, WorkerSettings(block_seconds=0.2))
    task = asyncio.create_task(worker.run(stop))
    try:
        request = None
        for _ in range(1800):
            request = next((r for r in runtime.approvals.pending() if r.run_id == job.run_id), None)
            if request is not None:
                break
            await asyncio.sleep(0.05)
        assert request is not None
        assert drop_every_connection() >= 2  # the worker's queue and claim connections
        await asyncio.sleep(0.5)  # well inside the TTL: the worker must not fence itself off
        assert worker.running, "a short drop must not stop the run"
        runtime.approvals.decide(
            request.proposal_id,
            decision="approved",
            proposal_digest=request.proposal_digest,
            actor="operator:test",
        )
        done = None
        for _ in range(1800):
            try:
                done = queue.get(job.job_id)
            except RedisUnavailable:
                done = None
            if done is not None and done.terminal:
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=60)
    assert done is not None and (done.status, done.category) == ("completed", "RESOLVED")
    assert queue.alive(worker.worker_id) is False  # it unregistered as it stopped
