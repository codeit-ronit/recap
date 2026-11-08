"""The Redis job queue (M6 W3) and its RESP client, against the in-repo fake Redis server.

What must hold: a submission is idempotent and refused when the queue is full, two workers
never claim the same job, a heartbeat that expired reads as a dead worker and cannot be
refreshed (fencing), and every multi-key change is one MULTI/EXEC.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from recap.core.errors import ConfigError
from recap.workers.queue import IdempotencyConflict, JobQueue, QueueConfig, QueueFull
from recap.workers.resp import Redis, RedisAddress, RedisError, RedisUnavailable
from tests.fake_redis import FakeRedisServer, free_port


@pytest.fixture
def server() -> Iterator[FakeRedisServer]:
    with FakeRedisServer.running() as running:
        yield running


def queue_on(server: FakeRedisServer, **overrides: object) -> JobQueue:
    config = QueueConfig(url=server.url, heartbeat_seconds=0.1, heartbeat_ttl_seconds=0.4)
    for name, value in overrides.items():
        object.__setattr__(config, name, value)
    return JobQueue(config)


def submit(queue: JobQueue, key: str = "", scenario: str = "bad-deployment") -> tuple[str, bool]:
    job, created = queue.submit(
        run_id=f"run-{time.monotonic_ns()}",
        scenario_id=scenario,
        config={"approval_mode": "manual"},
        idempotency_key=key,
        request_digest=scenario,
    )
    return job.job_id, created


# --- the client -----------------------------------------------------------------------------


def test_the_client_speaks_resp_and_tells_errors_from_outages(server: FakeRedisServer) -> None:
    r = Redis(server.url)
    assert r.ping()
    assert r.set("k", "v", px=10_000) and r.get("k") == "v"
    assert not r.set("k", "w", nx=True) and r.get("k") == "v"  # NX: only when absent
    assert not r.set("missing", "x", xx=True) and r.get("missing") is None  # XX: only present
    r.hset("h", {"a": "1", "b": "two"})
    assert r.hgetall("h") == {"a": "1", "b": "two"} and r.hincrby("h", "a", 4) == 5
    r.rpush("l", "x", "y")
    assert r.lrange("l", 0, -1) == ["x", "y"] and r.lrem("l", 0, "x") == 1
    assert r.blmove("empty", "dest", 0.1) is None  # waits its timeout, then answers null
    replies = r.pipeline([("GET", "k"), ("HGET", "k", "f")])
    assert replies[0] == "v" and isinstance(replies[1], RedisError)  # WRONGTYPE, returned
    with pytest.raises(RedisError):
        r.execute("NOSUCHCOMMAND")
    server.stop()
    with pytest.raises(RedisUnavailable):
        r.get("k")  # the connection broke: an outage, not a refusal
    restarted = server.restarted()
    try:
        assert r.get("k") == "v"  # reconnects by itself, the data intact
    finally:
        restarted.stop()


def test_a_key_with_a_ttl_expires() -> None:
    with FakeRedisServer.running() as server:
        r = Redis(server.url)
        r.set("beat", "1", px=100)
        assert r.exists("beat")
        time.sleep(0.15)
        assert not r.exists("beat") and r.get("beat") is None


def test_an_unreachable_server_is_an_outage_and_a_password_is_never_shown() -> None:
    url = f"redis://:s3cret@127.0.0.1:{free_port()}/2"
    assert RedisAddress.parse(url).shown() == RedisAddress.parse(url).shown().replace("s3cret", "")
    assert "s3cret" not in RedisAddress.parse(url).shown()
    with pytest.raises(RedisUnavailable) as raised:
        Redis(url, timeout=0.5).ping()
    assert "s3cret" not in str(raised.value)
    with pytest.raises(ValueError):
        RedisAddress.parse("http://127.0.0.1:6379")


def test_a_transaction_runs_together_and_raises_any_error(server: FakeRedisServer) -> None:
    r = Redis(server.url)
    assert r.transaction([("SET", "b", "1"), ("LPUSH", "q", "x")]) == ["OK", 1]
    r.set("s", "text")
    with pytest.raises(RedisError):  # a command that fails as it runs is raised, not hidden
        r.transaction([("SET", "a", "1"), ("HSET", "s", "f", "v")])


# --- configuration ------------------------------------------------------------------------------


def test_the_in_process_backend_is_the_default() -> None:
    assert QueueConfig.from_environment({}) is None
    assert QueueConfig.from_environment({"RECAP_EXECUTION_BACKEND": "inprocess"}) is None
    config = QueueConfig.from_environment(
        {
            "RECAP_EXECUTION_BACKEND": "redis",
            "RECAP_REDIS_URL": "redis://redis:6379/0",
            "RECAP_QUEUE_CAPACITY": "5",
            "RECAP_JOB_MAX_ATTEMPTS": "2",
        }
    )
    assert config is not None and (config.url, config.capacity, config.max_attempts) == (
        "redis://redis:6379/0",
        5,
        2,
    )
    for bad in (
        {"RECAP_EXECUTION_BACKEND": "celery"},
        {"RECAP_EXECUTION_BACKEND": "redis", "RECAP_QUEUE_CAPACITY": "lots"},
        {
            "RECAP_EXECUTION_BACKEND": "redis",
            "RECAP_WORKER_HEARTBEAT_SECONDS": "10",
            "RECAP_WORKER_HEARTBEAT_TTL_SECONDS": "5",
        },
    ):
        with pytest.raises(ConfigError):
            QueueConfig.from_environment(bad)


# --- submitting ---------------------------------------------------------------------------------


def test_an_idempotency_key_names_one_submission(server: FakeRedisServer) -> None:
    queue = queue_on(server)
    first, created = submit(queue, key="client-1")
    again, created_again = submit(queue, key="client-1")
    assert created and not created_again and again == first
    assert queue.depth() == 1  # nothing new was queued
    with pytest.raises(IdempotencyConflict):
        submit(queue, key="client-1", scenario="another_scenario")
    with pytest.raises(ValueError):
        submit(queue, key="has spaces/and slashes")
    other, _ = submit(queue)  # no key: a new job every time
    assert other != first and queue.depth() == 2


def test_a_full_queue_refuses_a_submission(server: FakeRedisServer) -> None:
    queue = queue_on(server, capacity=2)
    first, _ = submit(queue)
    submit(queue)
    with pytest.raises(QueueFull):
        submit(queue)
    assert queue.depth() == 2 and queue.position(first) == 1  # the oldest is claimed next
    snapshot = queue.snapshot()
    assert snapshot["depth"] == 2 and snapshot["capacity"] == 2
    assert snapshot["counters"]["submitted"] == 2


# --- claiming -----------------------------------------------------------------------------------


def test_two_workers_never_claim_the_same_job(server: FakeRedisServer) -> None:
    queue = queue_on(server, capacity=50)
    submitted = {submit(queue)[0] for _ in range(30)}
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        connection = queue.connection()
        while True:
            job = queue.claim(name, block_seconds=0.2, redis=connection)
            if job is None:
                return
            with lock:
                claimed.append(job.job_id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert sorted(claimed) == sorted(submitted)  # each exactly once
    held = [job_id for i in range(4) for job_id in queue.held_by(f"w{i}")]
    assert sorted(held) == sorted(submitted)  # every claimed job sits in its worker's list
    job = queue.get(claimed[0])
    assert job is not None and job.status == "running" and job.attempt == 1


def test_a_job_cancelled_while_waiting_is_never_claimed(server: FakeRedisServer) -> None:
    queue = queue_on(server)
    job_id, _ = submit(queue)
    job = queue.get(job_id)
    assert job is not None and queue.request_cancel(job) == "cancelled"
    assert queue.depth() == 0 and queue.claim("w", block_seconds=0.1) is None
    cancelled = queue.get(job_id)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert cancelled.category == "cancelled_before_start"
    assert queue.request_cancel(cancelled) == "finished"


def test_a_running_job_is_asked_to_stop(server: FakeRedisServer) -> None:
    queue = queue_on(server)
    job_id, _ = submit(queue)
    job = queue.claim("w", block_seconds=0.1)
    assert job is not None and queue.request_cancel(job) == "requested"
    assert queue.cancel_requested([job_id, "job-other"]) == {job_id}


def test_finish_requeue_and_unclaim_keep_the_lists_consistent(server: FakeRedisServer) -> None:
    queue = queue_on(server)
    job_id, _ = submit(queue)
    job = queue.claim("w", block_seconds=0.1)
    assert job is not None
    queue.unclaim(job, "w")  # claimed as the worker stopped: attempt given back
    back = queue.get(job_id)
    assert back is not None and back.attempt == 0 and queue.held_by("w") == []
    job = queue.claim("w", block_seconds=0.1)
    assert job is not None and job.attempt == 1
    queue.set_run(job, "run-second")
    queue.requeue(job, "w", category="worker_lost_before_write", detail="nothing was sent")
    again = queue.get(job_id)
    assert again is not None and again.status == "queued" and again.runs[-1] == "run-second"
    assert queue.job_for_run("run-second") is not None and queue.position(job_id) == 1
    job = queue.claim("w2", block_seconds=0.1)
    assert job is not None and job.attempt == 2 and job.run_id == "run-second"
    queue.finish(job, "w2", status="completed", category="RESOLVED", detail="recovered")
    done = queue.get(job_id)
    assert done is not None and done.terminal and done.finished_at
    assert queue.held_by("w2") == [] and queue.snapshot()["counters"]["completed"] == 1


# --- workers ------------------------------------------------------------------------------------


def test_an_expired_heartbeat_is_a_dead_worker_and_cannot_be_refreshed(
    server: FakeRedisServer,
) -> None:
    queue = queue_on(server)
    queue.register("w", {"pid": 1})
    assert queue.heartbeat("w", {"pid": 1}) and queue.alive("w")
    assert queue.workers() == {"w": {"pid": 1}}
    time.sleep(0.5)  # past the 0.4 s TTL
    assert queue.workers() == {"w": None} and not queue.alive("w")
    assert not queue.heartbeat("w", {"pid": 1})  # fencing: it must stop, not carry on
    queue.forget("w")
    assert queue.workers() == {}


def test_one_process_at_a_time_settles_a_job(server: FakeRedisServer) -> None:
    queue = queue_on(server)
    assert queue.lock("job-1", "worker-a")
    assert not queue.lock("job-1", "worker-b")
    queue.unlock("job-1")
    assert queue.lock("job-1", "worker-b")
