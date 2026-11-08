"""The seven crash windows (M6 W3): a worker process killed with SIGKILL at each one, and a
second worker process settling what it left behind.

Each case starts `python -m apps.worker` with RECAP_FAULT_INJECTION=1 and RECAP_FAULT_POINT set:
the worker kills itself at exactly that point (recap/workers/faults.py), with no cleanup, as an
out-of-memory kill would. A second worker, with no fault, then recovers the job from the run's
persisted evidence (recap/workers/reconcile.py). The test process plays the person: it approves
through the shared approval store, as the API does. Redis is tests/fake_redis.py, over TCP.

What must hold after every crash:

  * no write is ever applied twice: each run's simulated world logs every authorized write
  * a write that may have reached the target is never retried: the job needs reconciliation
  * an approval is used at most once, and a voided one can never be used
  * a retry is a new run that investigates afresh and asks for a fresh approval
  * the interrupted run's trace keeps every event, gapless, and ends with its run.summary
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import REPO_ROOT, Settings
from recap.core.errors import ApprovalError
from recap.core.interfaces import TargetEnvironment
from recap.core.models import ApprovalRequest, ApprovalStatus, IncidentState, WriteEvidence
from recap.environments.registry import create_environment
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory
from recap.workers.queue import Job, JobQueue, QueueConfig
from recap.workers.reconcile import recover_dead_workers
from tests.fake_redis import FakeRedisServer

pytestmark = pytest.mark.e2e  # real worker processes, the MCP subprocess, the simulator

HEARTBEAT, TTL = 0.2, 1.0


@pytest.fixture
def server() -> Iterator[FakeRedisServer]:
    with FakeRedisServer.running() as running:
        yield running


class Workers:
    """Starts worker processes on the test's data folder and Redis, and reaps them."""

    def __init__(self, settings: Settings, server: FakeRedisServer, logs: Path) -> None:
        self.settings, self.server, self.logs = settings, server, logs
        self.processes: list[tuple[subprocess.Popen[bytes], Path]] = []

    def start(self, fault: str | None = None) -> subprocess.Popen[bytes]:
        env = {
            **os.environ,
            "RECAP_DATA_DIR": str(self.settings.paths.data_dir),
            "RECAP_TOOL_LATENCY_MS": "0",
            "RECAP_EXECUTION_BACKEND": "redis",
            "RECAP_REDIS_URL": self.server.url,
            "RECAP_WORKER_HEARTBEAT_SECONDS": str(HEARTBEAT),
            "RECAP_WORKER_HEARTBEAT_TTL_SECONDS": str(TTL),
            "RECAP_WORKER_BLOCK_SECONDS": "0.2",
            "RECAP_WORKER_GRACE_SECONDS": "1",
        }
        if fault is not None:
            env |= {"RECAP_FAULT_INJECTION": "1", "RECAP_FAULT_POINT": fault}
        log = self.logs / f"worker-{len(self.processes)}-{fault or 'rescuer'}.log"
        with log.open("wb") as out:
            process = subprocess.Popen(
                [sys.executable, "-m", "apps.worker"],
                cwd=REPO_ROOT,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        self.processes.append((process, log))
        return process

    def killed(self, process: subprocess.Popen[bytes], timeout: float = 90) -> None:
        """Wait for a faulted worker to kill itself (SIGKILL: exit code -9)."""
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"the worker never reached its fault point\n{self.tail()}"
            ) from None
        assert code == -signal.SIGKILL, f"exit code {code}\n{self.tail()}"

    def tail(self) -> str:
        return "\n".join(f"--- {log.name}\n{log.read_text()[-3000:]}" for _, log in self.processes)

    def stop_all(self) -> None:
        for process, _ in self.processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process, _ in self.processes:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture
def workers(settings: Settings, server: FakeRedisServer, tmp_path: Path) -> Iterator[Workers]:
    started = Workers(settings, server, tmp_path)
    try:
        yield started
    finally:
        started.stop_all()


def make_queue(server: FakeRedisServer) -> JobQueue:
    return JobQueue(
        QueueConfig(url=server.url, heartbeat_seconds=HEARTBEAT, heartbeat_ttl_seconds=TTL)
    )


def submit(queue: JobQueue, run_id: str) -> Job:
    job, _ = queue.submit(
        run_id=run_id, scenario_id="bad-deployment", config={"approval_mode": "manual"}
    )
    return job


def wait_for(predicate: Callable[[], Any], what: str, workers: Workers, timeout: float = 90) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"{what}: not in time\n{workers.tail()}")


def pending(runtime: Runtime, run_id: str) -> Callable[[], ApprovalRequest | None]:
    return lambda: next((r for r in runtime.approvals.pending() if r.run_id == run_id), None)


def approve(runtime: Runtime, request: ApprovalRequest) -> None:
    runtime.approvals.decide(
        request.proposal_id,
        decision="approved",
        proposal_digest=request.proposal_digest,
        actor="operator:test",
    )


def applied_writes(runtime: Runtime, run_id: str) -> list[dict[str, Any]]:
    """The writes the run's simulated world applied (its own action log, read directly)."""
    environment = runtime.environment_factory(runtime.settings.environment, run_id)
    state = environment.store.load() or {}  # type: ignore[attr-defined]
    return [entry for entry in state.get("action_log", []) if entry.get("ok")]


def ended(queue: JobQueue, job: Job) -> Callable[[], Job | None]:
    def check() -> Job | None:
        found = queue.get(job.job_id)
        return found if found is not None and found.terminal else None

    return check


def trace_is_whole(runtime: Runtime, run_id: str) -> list[Any]:
    events = runtime.events.list_events(run_id)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))  # nothing lost or doubled
    assert events[-1].category == TraceCategory.RUN_SUMMARY
    return events


def finish_retry(
    runtime: Runtime, queue: JobQueue, job: Job, workers: Workers
) -> tuple[Job, ApprovalRequest]:
    """The rescuer runs the job's next attempt: a new run that asks a person afresh."""
    again = wait_for(
        lambda: (j := queue.get(job.job_id)) and len(j.runs) == 2 and j, "the retry", workers
    )
    fresh = wait_for(pending(runtime, again.run_id), "the retry's approval request", workers)
    approve(runtime, fresh)
    done = wait_for(ended(queue, job), "the retry's end", workers)
    return done, fresh


# --- window 1: before the claim --------------------------------------------------------------


def test_window_1_a_worker_killed_before_it_claims_leaves_the_job_queued(
    settings: Settings, server: FakeRedisServer, workers: Workers
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    idle = workers.start()
    wait_for(lambda: queue.workers(), "the worker's registration", workers)
    idle.send_signal(signal.SIGKILL)
    idle.wait(timeout=30)
    time.sleep(0.5)  # past its last blocking claim: nothing can be in flight
    job = submit(queue, "run-window-1")
    assert queue.depth() == 1
    workers.start()
    approve(runtime, wait_for(pending(runtime, job.run_id), "the approval request", workers))
    done = wait_for(ended(queue, job), "the job's end", workers)
    assert (done.status, done.attempt, done.runs) == ("completed", 1, [job.run_id])
    assert len(applied_writes(runtime, job.run_id)) == 1
    wait_for(lambda: len(queue.workers()) == 1, "the dead worker forgotten", workers)


# --- window 2: claimed, the model not called yet ------------------------------------------------


def test_window_2_killed_after_the_claim_the_same_run_runs_again(
    settings: Settings, server: FakeRedisServer, workers: Workers
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    job = submit(queue, "run-window-2")
    workers.killed(workers.start("after_claim"))
    held = queue.get(job.job_id)
    assert held is not None and held.status == "running" and runtime.runs.get(job.run_id) is None
    workers.start()
    approve(runtime, wait_for(pending(runtime, job.run_id), "the approval request", workers))
    done = wait_for(ended(queue, job), "the job's end", workers)
    # Nothing had started, so the same run id runs, as the job's second attempt.
    assert (done.status, done.attempt, done.runs) == ("completed", 2, [job.run_id])
    assert done.category == "RESOLVED" and len(applied_writes(runtime, job.run_id)) == 1


# --- window 3: during the investigation --------------------------------------------------------


def test_window_3_killed_while_investigating_a_new_run_starts_afresh(
    settings: Settings, server: FakeRedisServer, workers: Workers
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    job = submit(queue, "run-window-3")
    workers.killed(workers.start("investigating"))
    workers.start()
    done, _ = finish_retry(runtime, queue, job, workers)
    assert done.status == "completed" and done.attempt == 2
    old = runtime.runs.get(job.run_id)
    assert old is not None and old.state == IncidentState.FAILED and old.interruption is not None
    assert (old.interruption.phase, old.interruption.cause) == ("before_write", "worker_lost")
    assert old.interruption.retried_as == done.run_id and not old.interruption.needs_reconciliation
    closing = trace_is_whole(runtime, job.run_id)
    summary = closing[-1]
    assert summary.payload["interrupted"]["retried_as"] == done.run_id
    # The move to FAILED has one time, in the record and in the trace.
    assert closing[-2].payload["to"] == "FAILED" and closing[-2].ts == old.history[-1].at
    assert applied_writes(runtime, job.run_id) == []
    assert len(applied_writes(runtime, done.run_id)) == 1


# --- window 4: waiting for a person -----------------------------------------------------------


def test_window_4_killed_while_awaiting_approval_the_request_is_voided(
    settings: Settings, server: FakeRedisServer, workers: Workers
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    job = submit(queue, "run-window-4")
    workers.killed(workers.start("awaiting_approval"))
    first = wait_for(pending(runtime, job.run_id), "the dead run's request", workers)
    workers.start()
    done, fresh = finish_retry(runtime, queue, job, workers)
    assert done.status == "completed" and fresh.proposal_id != first.proposal_id
    voided = runtime.approvals.get_request(first.proposal_id)
    assert voided is not None and voided.status == ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalError):
        approve(runtime, first)  # a late "yes" for the dead run authorizes nothing
    old = runtime.runs.get(job.run_id)
    assert old is not None and old.interruption is not None
    assert old.interruption.phase == "awaiting_approval"
    trace_is_whole(runtime, job.run_id)
    assert applied_writes(runtime, job.run_id) == []


# --- window 5: right before the authorized write -----------------------------------------------


def test_window_5_killed_before_the_write_nothing_was_applied_and_the_approval_stays_used(
    settings: Settings, server: FakeRedisServer, workers: Workers
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    job = submit(queue, "run-window-5")
    crasher = workers.start("before_write")
    first = wait_for(pending(runtime, job.run_id), "the approval request", workers)
    approve(runtime, first)
    workers.killed(crasher)
    # The gateway used the approval and wrote the write-ahead record, then the process died
    # before sending: the simulator's action log proves nothing was applied.
    events = runtime.events.list_events(job.run_id)
    assert events[-1].category == TraceCategory.MITIGATION_STARTED
    approval_id = events[-1].payload["approval_id"]
    used = runtime.approvals.get_approval(approval_id)
    assert used is not None and used.consumed_at is not None
    workers.start()
    done, fresh = finish_retry(runtime, queue, job, workers)
    assert done.status == "completed" and fresh.proposal_id != first.proposal_id
    with pytest.raises(ApprovalError):
        runtime.approvals.consume(approval_id)  # single use, even across the crash
    old = runtime.runs.get(job.run_id)
    assert old is not None and old.interruption is not None
    assert (old.interruption.phase, old.interruption.write) == ("write_in_flight", "not_applied")
    trace_is_whole(runtime, job.run_id)
    assert applied_writes(runtime, job.run_id) == []
    assert len(applied_writes(runtime, done.run_id)) == 1


def test_window_5_on_a_target_that_cannot_tell_the_run_is_never_retried(
    settings: Settings, server: FakeRedisServer, workers: Workers
) -> None:
    """Fail closed: the same crash, settled against a target without a readable audit log
    (as a real cluster is when its audit row is missing), needs a person."""
    queue = make_queue(server)
    job = submit(queue, "run-window-5-unknown")
    crasher = workers.start("before_write")
    runtime = Runtime(settings)
    approve(runtime, wait_for(pending(runtime, job.run_id), "the approval request", workers))
    workers.killed(crasher)

    class CannotTell:
        def __init__(self, inner: TargetEnvironment) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        def recorded_write(self, approval_id: str, proposal_digest: str) -> WriteEvidence:
            return WriteEvidence(state="unknown", detail="no audit log")

    blind = Runtime(
        settings,
        environment_factory=lambda env, run_id: CannotTell(create_environment(env, run_id)),
    )
    time.sleep(TTL + 0.3)  # the dead worker's heartbeat expires
    done = recover_dead_workers(blind, queue, "worker-test")
    assert len(done) == 1 and "a person checks the target" in done[0]
    settled = queue.get(job.job_id)
    assert settled is not None and settled.status == "needs_reconciliation"
    assert settled.runs == [job.run_id]  # not retried
    old = blind.runs.get(job.run_id)
    assert old is not None and old.interruption is not None
    assert old.interruption.needs_reconciliation and old.interruption.write == "unknown"


# --- window 6: the write applied, its outcome not recorded --------------------------------------


def test_window_6_killed_after_the_write_it_is_never_sent_again(
    settings: Settings, server: FakeRedisServer, workers: Workers
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    job = submit(queue, "run-window-6")
    crasher = workers.start("after_write")
    approve(runtime, wait_for(pending(runtime, job.run_id), "the approval request", workers))
    workers.killed(crasher)
    assert len(applied_writes(runtime, job.run_id)) == 1  # the target applied it
    assert runtime.events.list_events(job.run_id)[-1].category == TraceCategory.MITIGATION_STARTED
    workers.start()
    done = wait_for(ended(queue, job), "the job settled", workers)
    assert done.status == "needs_reconciliation" and done.runs == [job.run_id]
    assert done.category == "worker_lost_write_in_flight"
    old = runtime.runs.get(job.run_id)
    assert old is not None and old.interruption is not None
    assert (old.interruption.write, old.interruption.needs_reconciliation) == ("applied", True)
    trace_is_whole(runtime, job.run_id)
    time.sleep(1.0)  # the rescuer stays up: it must not start anything for this job
    assert len(applied_writes(runtime, job.run_id)) == 1  # still exactly one
    assert not [r for r in runtime.approvals.pending() if r.incident_id == old.incident.incident_id]  # type: ignore[union-attr]


# --- window 7: verifying the recovery ----------------------------------------------------------


def test_window_7_killed_while_verifying_the_fix_stands_and_a_person_checks(
    settings: Settings, server: FakeRedisServer, workers: Workers
) -> None:
    runtime, queue = Runtime(settings), make_queue(server)
    job = submit(queue, "run-window-7")
    crasher = workers.start("verifying")
    approve(runtime, wait_for(pending(runtime, job.run_id), "the approval request", workers))
    workers.killed(crasher)
    workers.start()
    done = wait_for(ended(queue, job), "the job settled", workers)
    assert done.status == "needs_reconciliation" and done.runs == [job.run_id]
    assert done.category == "worker_lost_verifying"
    old = runtime.runs.get(job.run_id)
    assert old is not None and old.interruption is not None
    assert (old.interruption.phase, old.interruption.write) == ("verifying", "applied")
    events = trace_is_whole(runtime, job.run_id)
    assert any(e.category == TraceCategory.MITIGATION and e.payload.get("ok") for e in events)
    assert len(applied_writes(runtime, job.run_id)) == 1
