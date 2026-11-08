"""The job queue and a worker against a REAL Redis server (M6 W3).

Skipped unless RECAP_TEST_REDIS_URL names one (Redis 6.2 or newer, for BLMOVE), e.g.

    docker compose -f infra/compose/compose.yaml up -d redis
    export RECAP_TEST_REDIS_URL=redis://127.0.0.1:6379/15
    .venv/bin/pytest tests/integration/test_workers_redis.py

Every key is written under a fresh prefix, and deleted afterwards, so the test never touches
another prefix's jobs. The rest of the W3 tests use tests/fake_redis.py, which implements the
same commands; this one checks that the real server agrees with it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator

import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState
from recap.runtime import Runtime
from recap.workers.queue import JobQueue, QueueConfig, QueueFull
from recap.workers.resp import Redis
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
