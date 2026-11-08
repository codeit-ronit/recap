"""Workers run queued incidents through RECAP's own harness (M6 W3).

Each test runs a real Runtime (a temporary data folder, the simulator, the real MCP tool server
subprocess and the safety gateway) inside a Worker on this event loop, with the in-repo fake
Redis (tests/fake_redis.py) as the queue. A person's approval goes through the shared approval
store, as the API sends it. The crash windows, with real processes killed by SIGKILL, are in
tests/integration/test_worker_crashes.py.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from recap.core.config import Settings
from recap.core.errors import ApprovalError
from recap.core.models import TERMINAL_STATES, ApprovalRequest, ApprovalStatus, IncidentState
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory
from recap.workers.queue import Job, JobQueue, QueueConfig
from recap.workers.resp import RedisError
from recap.workers.worker import Worker, WorkerSettings
from tests.fake_redis import FakeRedisServer

pytestmark = pytest.mark.e2e  # real incidents through the controller and the MCP subprocess


@pytest.fixture
def server() -> Iterator[FakeRedisServer]:
    with FakeRedisServer.running() as running:
        yield running


def make_queue(server: FakeRedisServer, **overrides: Any) -> JobQueue:
    fields: dict[str, Any] = {
        "url": server.url,
        "heartbeat_seconds": 0.1,
        "heartbeat_ttl_seconds": 0.8,
        "capacity": 10,
    }
    return JobQueue(QueueConfig(**{**fields, **overrides}))


def submit(queue: JobQueue, *, approval: str = "auto", scenario: str = "bad-deployment") -> Job:
    job, created = queue.submit(
        run_id=f"run-{len(queue.redis.lrange(queue.queue_key, 0, -1))}-{id(object())}",
        scenario_id=scenario,
        config={"approval_mode": approval},
    )
    assert created
    return job


async def wait_for(predicate: Callable[[], Any], timeout: float = 90.0) -> Any:
    """Poll until `predicate` returns something truthy (Redis being away counts as "not yet")."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            value = predicate()
            if inspect.isawaitable(value):
                value = await value
        except RedisError:
            value = None
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


@asynccontextmanager
async def working(
    runtime: Runtime, queue: JobQueue, *, admit: Any = None, **settings: Any
) -> AsyncIterator[Worker]:
    stop = asyncio.Event()
    worker = Worker(runtime, queue, WorkerSettings(block_seconds=0.1, **settings), admit=admit)
    task = asyncio.create_task(worker.run(stop))
    try:
        yield worker
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=90)


def ended(queue: JobQueue, job: Job) -> Callable[[], Job | None]:
    def check() -> Job | None:
        found = queue.get(job.job_id)
        return found if found is not None and found.terminal else None

    return check


def pending(runtime: Runtime, run_id: str) -> Callable[[], ApprovalRequest | None]:
    return lambda: next((r for r in runtime.approvals.pending() if r.run_id == run_id), None)


def approve(runtime: Runtime, request: ApprovalRequest) -> None:
    runtime.approvals.decide(
        request.proposal_id,
        decision="approved",
        proposal_digest=request.proposal_digest,
        actor="operator:test",
    )


def status_of(runtime: Runtime, request: ApprovalRequest) -> ApprovalStatus:
    found = runtime.approvals.get_request(request.proposal_id)
    assert found is not None
    return found.status


# --- running ------------------------------------------------------------------------------------


async def test_a_worker_runs_a_queued_incident_through_the_harness(
    settings: Settings, server: FakeRedisServer
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    job = submit(queue)
    async with working(runtime, queue):
        done = await wait_for(ended(queue, job))
    assert done.status == "completed" and done.category == "RESOLVED"  # the run's own state
    assert done.runs == [job.run_id] and done.attempt == 1
    record = runtime.runs.get(job.run_id)
    assert (
        record is not None
        and record.state == IncidentState.RESOLVED
        and record.interruption is None
    )
    events = runtime.events.list_events(job.run_id)
    assert events[-1].category == TraceCategory.RUN_SUMMARY
    assert [e.seq for e in events] == list(range(1, len(events) + 1))  # gapless, in order
    assert queue.workers() == {}  # it unregistered as it stopped


async def test_a_worker_runs_at_most_its_concurrency_and_a_person_approves_each_fix(
    settings: Settings, server: FakeRedisServer
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    jobs = [submit(queue, approval="manual") for _ in range(3)]
    async with working(runtime, queue, concurrency=2) as worker:
        waiting = await wait_for(
            lambda: (
                [r for r in runtime.approvals.pending() if r.run_id in {j.run_id for j in jobs}]
                if len(runtime.approvals.pending()) >= 2
                else None
            )
        )
        assert len(waiting) == 2 and len(worker.running) == 2  # bounded: the third waits
        assert queue.depth() == 1
        for request in waiting:
            approve(runtime, request)  # through the shared store, as the API does
        third = next(j for j in jobs if j.run_id not in {r.run_id for r in waiting})
        approve(runtime, await wait_for(pending(runtime, third.run_id)))
        finished = [await wait_for(ended(queue, job)) for job in jobs]
    assert [job.status for job in finished] == ["completed"] * 3
    assert {job.category for job in finished} == {"RESOLVED"}


async def test_a_job_written_to_redis_by_anything_but_the_api_is_checked_again(
    settings: Settings, server: FakeRedisServer
) -> None:
    """The worker re-checks every job as the API checks a request (apps/worker's admission):
    a job that asks what POST /incidents/run refuses never starts a run."""
    from apps.worker.__main__ import admission

    runtime, queue = Runtime(settings), make_queue(server)
    forged = [
        queue.submit(
            run_id="run-forged-1", scenario_id="bad-deployment", config={"safety_enabled": False}
        )[0],
        queue.submit(run_id="run-forged-2", scenario_id="no-such-scenario", config={})[0],
        queue.submit(
            run_id="run-forged-3", scenario_id="bad-deployment", config={"provider": "anthropic"}
        )[0],
        queue.submit(
            run_id="run-forged-4", scenario_id="bad-deployment", config={"strategy": "nonsense"}
        )[0],
    ]
    async with working(runtime, queue, admit=admission):
        refused = [await wait_for(ended(queue, job)) for job in forged]
    assert [(job.status, job.category) for job in refused] == [("failed", "refused")] * 4
    assert "safety cannot be disabled" in (refused[0].detail or "")
    assert all(runtime.runs.get(job.run_id) is None for job in forged)  # nothing ran


# --- cancelling ---------------------------------------------------------------------------------


async def test_cancelling_a_queued_job_and_a_running_one(
    settings: Settings, server: FakeRedisServer
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    first, second = submit(queue, approval="manual"), submit(queue, approval="manual")
    async with working(runtime, queue, concurrency=1):
        request = await wait_for(pending(runtime, first.run_id))
        second_job, first_job = queue.get(second.job_id), queue.get(first.job_id)
        assert second_job is not None and first_job is not None
        assert queue.request_cancel(second_job) == "cancelled"  # off the queue: never runs
        assert queue.request_cancel(first_job) == "requested"  # the worker stops it
        done = await wait_for(ended(queue, first))
    assert done.status == "cancelled"
    record = runtime.runs.get(first.run_id)
    assert record is not None and record.state == IncidentState.FAILED
    assert record.outcome_note == "run cancelled" and record.interruption is None
    assert status_of(runtime, request) == ApprovalStatus.EXPIRED  # nobody can approve it now
    assert runtime.runs.get(second.run_id) is None
    never = queue.get(second.job_id)
    assert never is not None and never.status == "cancelled" and never.attempt == 0


# --- stopping and recovering -------------------------------------------------------------------


async def test_a_stopping_worker_hands_its_run_back_and_the_retry_asks_a_person_again(
    settings: Settings, server: FakeRedisServer
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    job = submit(queue, approval="manual")
    async with working(runtime, queue, grace_seconds=0.2):
        first_request = await wait_for(pending(runtime, job.run_id))
    # stopped while the run waited for a person: cancelled, then settled from its evidence
    again = queue.get(job.job_id)
    assert again is not None and again.status == "queued" and len(again.runs) == 2
    assert again.category == "worker_stopped_awaiting_approval"
    old = runtime.runs.get(job.run_id)
    assert old is not None and old.state == IncidentState.FAILED and old.interruption is not None
    assert (old.interruption.cause, old.interruption.phase) == (
        "worker_stopped",
        "awaiting_approval",
    )
    assert old.interruption.retried_as == again.run_id and not old.interruption.needs_reconciliation
    assert status_of(runtime, first_request) == ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalError):
        approve(runtime, first_request)  # the old request can never authorize anything
    async with working(runtime, queue):
        fresh = await wait_for(pending(runtime, again.run_id))
        assert fresh.proposal_id != first_request.proposal_id  # a fresh approval, for its own fix
        approve(runtime, fresh)
        done = await wait_for(ended(queue, job))
    assert done.status == "completed" and done.attempt == 2
    retried = runtime.runs.get(again.run_id)
    assert retried is not None and retried.state == IncidentState.RESOLVED


async def test_a_dead_worker_s_job_is_settled_and_run_by_another(
    settings: Settings, server: FakeRedisServer
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    job = submit(queue)
    queue.register("worker-dead", {"pid": 0})
    assert queue.claim("worker-dead", block_seconds=0.1) is not None  # claimed, then it died
    await asyncio.sleep(queue.config.heartbeat_ttl_seconds + 0.2)  # its heartbeat expires
    async with working(runtime, queue):
        done = await wait_for(ended(queue, job))
    # It had not started: the same run id runs, as the job's second attempt.
    assert done.status == "completed" and done.runs == [job.run_id] and done.attempt == 2
    assert queue.workers() == {}  # the dead worker was forgotten once it held nothing


async def test_a_run_that_ended_before_its_worker_died_keeps_its_outcome(
    settings: Settings, server: FakeRedisServer
) -> None:
    """The worker died after the run's final checkpoint and before its run.summary (memory
    consolidation comes in between): the record says RESOLVED, so the job completed."""
    from recap.core.models import RunConfig, RunRecord
    from recap.workers.reconcile import recover_dead_workers

    runtime, queue = Runtime(settings), make_queue(server)
    job = submit(queue)
    queue.register("worker-dead", {"pid": 0})
    assert queue.claim("worker-dead", block_seconds=0.1) is not None
    runtime.runs.save(
        RunRecord(
            run_id=job.run_id,
            scenario_id="bad-deployment",
            config=RunConfig(),
            state=IncidentState.RESOLVED,
            outcome_note="recovered",
        )
    )
    await asyncio.sleep(queue.config.heartbeat_ttl_seconds + 0.2)
    done = recover_dead_workers(runtime, queue, "worker-test")
    assert done == [f"{job.job_id}: its run {job.run_id} had already ended (RESOLVED)"]
    settled = queue.get(job.job_id)
    assert settled is not None and (settled.status, settled.category) == ("completed", "RESOLVED")
    record = runtime.runs.get(job.run_id)
    assert record is not None and record.interruption is None  # not relabelled


# --- Redis going away ---------------------------------------------------------------------------


async def test_a_short_redis_outage_loses_no_outcome(
    settings: Settings, server: FakeRedisServer
) -> None:
    runtime = Runtime(settings)
    queue = make_queue(server, heartbeat_ttl_seconds=30.0)  # shorter than the fencing threshold
    job = submit(queue, approval="manual")
    restarted: FakeRedisServer | None = None
    try:
        async with working(runtime, queue) as worker:
            request = await wait_for(pending(runtime, job.run_id))
            server.stop()  # Redis goes away while the run waits for a person
            approve(runtime, request)  # approvals live in SQLite: no Redis involved
            record = await wait_for(
                lambda: (r := runtime.runs.get(job.run_id)) and r.state in TERMINAL_STATES and r
            )
            assert record.state == IncidentState.RESOLVED
            await wait_for(lambda: worker._unrecorded)  # the job's bookkeeping waits for Redis
            restarted = server.restarted()
            done = await wait_for(ended(queue, job))
        assert done.status == "completed" and done.category == "RESOLVED"
    finally:
        if restarted is not None:
            restarted.stop()


async def test_a_long_redis_outage_fences_the_worker_and_the_run_is_settled_after(
    settings: Settings, server: FakeRedisServer
) -> None:
    runtime, queue = Runtime(settings), make_queue(server, heartbeat_ttl_seconds=0.6)
    job = submit(queue, approval="manual")
    restarted: FakeRedisServer | None = None
    try:
        async with working(runtime, queue) as worker:
            await wait_for(pending(runtime, job.run_id))
            server.stop()
            # Silent past most of its TTL, the worker stops its run: another worker may soon
            # take the job over, and two processes must never act for one run.
            await wait_for(lambda: not worker.running, timeout=15)
            stopped = runtime.runs.get(job.run_id)
            assert stopped is not None and stopped.outcome_note == "run cancelled"
            restarted = server.restarted()
            again = await wait_for(lambda: (j := queue.get(job.job_id)) and len(j.runs) == 2 and j)
            approve(runtime, await wait_for(pending(runtime, again.run_id)))
            done = await wait_for(ended(queue, job))
        assert done.status == "completed"
        old = runtime.runs.get(job.run_id)
        assert old is not None and old.interruption is not None
        assert (old.interruption.cause, old.interruption.phase) == (
            "worker_stopped",
            "awaiting_approval",
        )
    finally:
        if restarted is not None:
            restarted.stop()
