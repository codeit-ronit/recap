"""The worker: claims queued incidents and runs them through RECAP's own harness.

    python -m apps.worker        (RECAP_EXECUTION_BACKEND=redis, RECAP_REDIS_URL=redis://...)

Each incident runs exactly as the API's in-process backend runs it, Runtime.run_incident with
the external approval channel, so a person approves through the API whichever process runs it
(the approval wait re-reads the shared approval store). One worker runs up to
RECAP_WORKER_CONCURRENCY incidents at once, and always one at a time on a live target.

    claim     atomic (BLMOVE); a claimed job sits in this worker's own processing list
    heartbeat every RECAP_WORKER_HEARTBEAT_SECONDS; silent past the TTL, a worker is dead
    cancel    the API marks the job; the next heartbeat cancels the run's task, and the
              controller ends the run as FAILED ("run cancelled") and expires its request
    recover   at start and every few heartbeats: the jobs of dead workers are settled from
              their runs' persisted evidence (reconcile.py): run again, or closed for a person;
              so are this worker's own leftovers (runs it stopped while Redis was away)
    fencing   a worker that cannot refresh its heartbeat before the TTL would run out, or
              finds it expired, cancels its own incidents: another worker may settle them
    shutdown  SIGTERM/SIGINT stop claiming; running incidents get the grace period to finish,
              then are cancelled (a write already sent is awaited and recorded by the gateway)
              and settled like the jobs of a dead worker, by this worker if Redis answers

Redis coordinates and never decides: a job's outcome is copied from its run's record, and when
Redis cannot be told, the record still holds it and the bookkeeping is retried. Nothing read from
Redis is trusted either: `admit` re-checks every job as the API checks a request (apps/worker
passes the API's own check_run_request), so a job written to Redis by anything but the API runs
only what the API would have started.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recap.core.ids import new_id, utcnow
from recap.core.models import RunConfig
from recap.runtime import Runtime
from recap.safety.approvals import ExternalApprovalChannel
from recap.workers.faults import FaultPlan, crash_after_write
from recap.workers.queue import Job, JobQueue, JobStatus
from recap.workers.reconcile import CANCELLED_NOTE, recover_dead_workers, settle
from recap.workers.resp import RedisUnavailable

log = logging.getLogger("recap.worker")

# Re-checks a job before it runs: returns the config to run, or raises with the reason.
Admission = Callable[[Runtime, str, RunConfig], RunConfig]

RECOVER_EVERY_BEATS = 3


@dataclass
class WorkerSettings:
    concurrency: int = 1
    block_seconds: float = 1.0  # how long one claim waits for a job
    grace_seconds: float = 25.0  # how long running incidents may finish after a stop signal
    health_file: str = ""  # touched after every heartbeat Redis accepted (container health)

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> WorkerSettings:
        env = dict(os.environ) if env is None else env
        return cls(
            concurrency=max(1, int(env.get("RECAP_WORKER_CONCURRENCY", "") or 1)),
            block_seconds=float(env.get("RECAP_WORKER_BLOCK_SECONDS", "") or 1),
            grace_seconds=float(env.get("RECAP_WORKER_GRACE_SECONDS", "") or 25),
            health_file=env.get("RECAP_WORKER_HEALTH_FILE", "").strip(),
        )


@dataclass
class _Running:
    job: Job
    task: asyncio.Task[None]
    cancel_requested: bool = False


def _worker_id() -> str:
    return f"worker-{socket.gethostname()}-{os.getpid()}-{new_id('w')[-6:]}"


@dataclass
class Worker:
    runtime: Runtime
    queue: JobQueue
    settings: WorkerSettings = field(default_factory=WorkerSettings)
    faults: FaultPlan | None = None
    admit: Admission | None = None  # apps/worker: the API's own check of a run request
    worker_id: str = field(default_factory=_worker_id)

    def __post_init__(self) -> None:
        self.running: dict[str, _Running] = {}
        self.started_at = utcnow().isoformat()
        self._last_beat = time.monotonic()
        self._beats = 0
        self._unrecorded: list[tuple[Job, JobStatus, str, str]] = []  # Redis did not answer
        # Held while a claim is in flight: a job moved into this worker's list but not running
        # yet is never mistaken for a leftover (_leftovers).
        self._claiming = asyncio.Lock()
        # Recovery runs beside the heartbeats, never inside them: a slow settling must not
        # delay a heartbeat past its TTL (the worker would then fence itself off).
        self._recovering: asyncio.Task[list[str]] | None = None
        # A live target admits one run at a time (its namespace lease), so one incident at once.
        if not self.runtime.environment_info().is_simulated:
            self.settings.concurrency = 1
        if self.faults is not None:  # crash tests only (RECAP_FAULT_INJECTION=1)
            sink = self.faults.sink()
            if sink is not None:
                self.runtime.extra_sinks.append(sink)
            if self.faults.point == "after_write":
                tools = self.runtime.settings.policies.remediation_tools
                self.runtime.transport_factory = crash_after_write(
                    self.runtime.transport_factory, tools
                )

    # --- the loop ---------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Claim and run incidents until `stop` is set, then shut down gracefully."""
        await self._redis(self.queue.register, self.worker_id, self._info())
        self._healthy()
        log.info("worker %s ready (concurrency %d)", self.worker_id, self.settings.concurrency)
        beats = asyncio.create_task(self._heartbeats(), name="heartbeats")  # before anything slow
        await self.recover()
        claims = self.queue.connection()  # BLMOVE blocks: a connection of its own
        pause = 0.5
        try:
            while not stop.is_set():
                if len(self.running) >= self.settings.concurrency:
                    await _sleep_unless(stop, 0.1)
                    continue
                async with self._claiming:
                    try:
                        job = await asyncio.to_thread(
                            self.queue.claim,
                            self.worker_id,
                            block_seconds=self.settings.block_seconds,
                            redis=claims,
                        )
                        pause = 0.5
                    except RedisUnavailable as exc:
                        job = None
                        log.warning(
                            "Redis unreachable, not claiming (retry in %.1f s): %s", pause, exc
                        )
                        await _sleep_unless(stop, pause)
                        pause = min(pause * 2, 10.0)
                    if job is None:
                        continue
                    if stop.is_set():  # claimed as the stop came: back to the front, attempt unused
                        await self._redis(self.queue.unclaim, job, self.worker_id)
                        break
                    task = asyncio.create_task(self._execute(job), name=job.job_id)
                    self.running[job.job_id] = _Running(job, task)
        finally:
            await self._shutdown()
            if self._recovering is not None:
                await asyncio.wait([self._recovering], timeout=30)
            beats.cancel()
            claims.close()

    async def _shutdown(self) -> None:
        tasks = [r.task for r in self.running.values()]
        if tasks:
            log.info(
                "stopping: %d incident(s) may finish within %.0f s",
                len(tasks),
                self.settings.grace_seconds,
            )
            await asyncio.wait(tasks, timeout=self.settings.grace_seconds)
        for running in list(self.running.values()):
            running.task.cancel()  # the controller ends the run; a sent write is awaited
        tasks = [r.task for r in self.running.values()]
        if tasks:
            await asyncio.wait(tasks, timeout=60)
        self._record_unrecorded()
        try:
            await self._redis(self.queue.unregister, self.worker_id)
        except RedisUnavailable:
            pass  # its heartbeat expires by itself; its jobs are then settled by another worker

    # --- one incident -------------------------------------------------------------------------

    async def _execute(self, job: Job) -> None:
        try:
            if self.runtime.runs.get(job.run_id) is not None:  # never run a run id twice
                await self._redis(self.queue.set_run, job, new_id("run"))
            if self.faults is not None:
                self.faults.at("after_claim")
            try:
                config = RunConfig.model_validate(job.config)
                if self.admit is not None:
                    config = self.admit(self.runtime, job.scenario_id, config)
            except Exception as exc:  # what the API would refuse never runs
                log.warning("job %s refused: %s", job.job_id, exc)
                await self._record(job, "failed", "refused", str(exc)[:500])
                return
            record = await self.runtime.run_incident(
                job.scenario_id,
                config,
                approval_channel=ExternalApprovalChannel(),
                run_id=job.run_id,
            )
        except asyncio.CancelledError:
            running = self.running.get(job.job_id)
            if running is not None and running.cancel_requested:
                await self._record(job, "cancelled", "cancelled", "a person cancelled the run")
                return  # what was asked for: the task ends normally
            # Stopped or fenced: settled like a dead worker's job, from the run's evidence.
            await _finish_anyway(self._settle_stopped, job)
            raise
        except Exception as exc:  # the harness raised before it could record an outcome
            log.exception("job %s failed", job.job_id)
            await self._record(job, "failed", "harness_error", f"{type(exc).__name__}: {exc}")
        else:
            status: JobStatus = (
                "cancelled" if record.outcome_note == CANCELLED_NOTE else "completed"
            )
            await self._record(job, status, record.state.value, record.outcome_note)
        finally:
            self.running.pop(job.job_id, None)

    async def _record(self, job: Job, status: JobStatus, category: str, detail: str) -> None:
        await _finish_anyway(self._finish, job, status, category, detail)

    def _finish(self, job: Job, status: JobStatus, category: str, detail: str) -> None:
        try:
            self.queue.finish(job, self.worker_id, status=status, category=category, detail=detail)
        except RedisUnavailable:
            # The run record (SQLite) holds the outcome; the job's bookkeeping is retried.
            log.warning("could not record job %s as %s: Redis is unreachable", job.job_id, status)
            self._unrecorded.append((job, status, category, detail))

    def _record_unrecorded(self) -> None:
        waiting, self._unrecorded = self._unrecorded, []
        for job, status, category, detail in waiting:
            self._finish(job, status, category, detail)

    def _settle_stopped(self, job: Job) -> None:
        try:
            outcome = settle(
                self.runtime, self.queue, job.job_id, self.worker_id, actor=self.worker_id
            )
            log.info("settled %s", outcome or f"{job.job_id}: another process holds its lock")
        except Exception as exc:  # Redis gone too: another worker settles it once this one is dead
            log.warning("could not settle job %s (%s); recovery will", job.job_id, exc)

    # --- heartbeats, cancellation, fencing, recovery -----------------------------------------

    async def _heartbeats(self) -> None:
        """Beat until cancelled; no failure ends the loop (a dead loop would let the key expire
        while the incidents run on)."""
        while True:
            await asyncio.sleep(self.queue.config.heartbeat_seconds)
            try:
                await self._beat()
            except RedisUnavailable:
                self._fence_if_silent()
            except Exception:
                log.exception("heartbeat step failed; retrying at the next beat")

    async def _beat(self) -> None:
        alive = await self._redis(self.queue.heartbeat, self.worker_id, self._info())
        self._last_beat = time.monotonic()
        fenced = not alive
        if fenced:
            log.error("heartbeat key expired: another worker may be settling this worker's jobs")
            self._fence()
            await self._redis(self.queue.register, self.worker_id, self._info())
        self._healthy()
        await asyncio.to_thread(self._record_unrecorded)
        await self._apply_cancellations()
        self._beats += 1
        if fenced or self._beats % RECOVER_EVERY_BEATS == 0:
            self._start_recovery()

    def _start_recovery(self) -> None:
        if self._recovering is None or self._recovering.done():
            self._recovering = asyncio.create_task(self.recover(), name="recover")

    def _fence_if_silent(self) -> None:
        """Fence before the key can expire: the next attempt would come too late."""
        config = self.queue.config
        silent = time.monotonic() - self._last_beat
        if self.running and silent + config.heartbeat_seconds >= 0.8 * config.heartbeat_ttl_seconds:
            log.error(
                "no heartbeat for %.1f s: stopping %d incident(s) before it expires",
                silent,
                len(self.running),
            )
            self._fence()

    def _fence(self) -> None:
        for running in self.running.values():
            running.task.cancel()

    async def _apply_cancellations(self) -> None:
        if not self.running:
            return
        try:
            asked = await self._redis(self.queue.cancel_requested, list(self.running))
        except RedisUnavailable:
            return
        for job_id in asked:
            running = self.running.get(job_id)
            if running is not None and not running.cancel_requested:
                running.cancel_requested = True
                running.task.cancel()

    async def recover(self) -> list[str]:
        """Settle the jobs of dead workers, and this worker's own leftovers; returns what was
        done (for logs and tests)."""
        try:
            done = await asyncio.to_thread(
                recover_dead_workers, self.runtime, self.queue, self.worker_id
            )
            for job_id in await self._leftovers():
                outcome = await asyncio.to_thread(
                    settle, self.runtime, self.queue, job_id, self.worker_id, actor=self.worker_id
                )
                if outcome is not None:
                    log.info("settled a leftover: %s", outcome)
                    done.append(outcome)
            return done
        except RedisUnavailable as exc:
            log.warning("recovery skipped, Redis is unreachable: %s", exc)
            return []

    async def _leftovers(self) -> list[str]:
        """Jobs in this worker's own list that it no longer runs or records: runs it stopped
        (a stop, or a fence) while Redis could not be told. Read with no claim in flight."""
        async with self._claiming:
            held = await self._redis(self.queue.held_by, self.worker_id)
            busy = set(self.running) | {job.job_id for job, *_ in self._unrecorded}
        return [job_id for job_id in held if job_id not in busy]

    # --- helpers -----------------------------------------------------------------------------

    def _info(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": self.started_at,
            "concurrency": self.settings.concurrency,
            "running": sorted(self.running),
            "target": self.runtime.settings.environment.kind,
        }

    def _healthy(self) -> None:
        if self.settings.health_file:
            Path(self.settings.health_file).touch()

    async def _redis(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(method, *args, **kwargs)


async def _finish_anyway(function: Any, *args: Any) -> None:
    """Run a blocking bookkeeping step in a thread; a second cancellation stops the wait for
    it, never the step itself."""
    await asyncio.shield(asyncio.to_thread(function, *args))


async def _sleep_unless(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
