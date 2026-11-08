"""The Redis job queue: who runs which incident, and nothing more.

    submit()   API      idempotent (an Idempotency-Key names one job), refused when full
    claim()    worker   atomic (BLMOVE from the queue to the worker's own processing list)
    heartbeat  worker   a key that expires: a worker silent past its TTL is dead
    finish()   worker   completed / failed / cancelled / needs_reconciliation
    requeue()  recovery a job whose attempt was interrupted before any write could happen

Keys (all under the configured prefix, "recap" by default):

    <p>:queue                 list   job ids waiting (new ones pushed left, claimed from the right)
    <p>:processing:<worker>   list   the jobs one worker holds (moved there by its claim)
    <p>:workers               set    every registered worker
    <p>:worker:<worker>       string the worker's heartbeat (JSON), with a TTL
    <p>:job:<job>             hash   the job (fields of Job)
    <p>:run:<run>             string the job a run belongs to
    <p>:idem:<key>            string the job an idempotency key names (expires after a day)
    <p>:lock:<job>            string held while one process reconciles an interrupted job
    <p>:recent                list   the newest job ids, for the queue view
    <p>:stats                 hash   counters (submitted, completed, failed, requeued, ...)

Delivery is at least once: a job held by a worker that dies is reconciled by another worker
(recap/workers/reconcile.py), which decides from the run's persisted evidence whether it may
run again. The queue never decides whether an incident or a fix succeeded: the run record does.

A job's status says only what happened to the job; `category` says why:

    completed             the harness ran the incident to its end; category: the run's final
                          state (RESOLVED, FAILED, BLOCKED, ...), which says how it went
    cancelled             cancelled_before_start (taken off the queue), or the person's cancel
                          reached the running incident (the run ends as FAILED "run cancelled")
    failed                harness_error (the harness raised before it recorded an outcome),
                          max_attempts (interrupted on every attempt)
    needs_reconciliation  worker_lost_<phase> / worker_stopped_<phase>: the attempt stopped after
                          a fix may have reached the target; never retried, a person checks
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from recap.core.errors import ConfigError
from recap.core.ids import new_id, utcnow
from recap.workers.resp import Redis

JobStatus = Literal["queued", "running", "completed", "failed", "cancelled", "needs_reconciliation"]
TERMINAL_JOB_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "needs_reconciliation"}
)

# What an Idempotency-Key may look like: it becomes part of a Redis key.
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")


@dataclass(frozen=True)
class QueueConfig:
    """How the Redis execution backend is configured (RECAP_EXECUTION_BACKEND=redis)."""

    url: str = "redis://127.0.0.1:6379/0"
    prefix: str = "recap"
    capacity: int = 20  # jobs waiting at most; a submission beyond it is refused (backpressure)
    max_attempts: int = 3  # attempts per job, counting the interrupted ones
    heartbeat_seconds: float = 5.0
    heartbeat_ttl_seconds: float = (
        20.0  # silent this long, a worker is dead: its jobs are reconciled
    )
    idempotency_ttl_seconds: int = 86_400

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> QueueConfig | None:
        """The Redis backend's settings, or None for the default in-process backend."""
        env = os.environ if env is None else env
        backend = env.get("RECAP_EXECUTION_BACKEND", "").strip().lower()
        if backend in ("", "inprocess", "in-process", "in_process"):
            return None
        if backend != "redis":
            raise ConfigError(
                f"RECAP_EXECUTION_BACKEND must be 'inprocess' or 'redis', not {backend!r}"
            )

        def number(name: str, default: float, low: float) -> float:
            raw = env.get(name, "").strip()
            try:
                value = float(raw) if raw else default
            except ValueError:
                raise ConfigError(f"{name} must be a number, not {raw!r}") from None
            if value < low:
                raise ConfigError(f"{name} must be at least {low:g}")
            return value

        heartbeat = number("RECAP_WORKER_HEARTBEAT_SECONDS", cls.heartbeat_seconds, 0.1)
        ttl = number("RECAP_WORKER_HEARTBEAT_TTL_SECONDS", cls.heartbeat_ttl_seconds, 0.5)
        if ttl <= heartbeat:
            raise ConfigError(
                "RECAP_WORKER_HEARTBEAT_TTL_SECONDS must be longer than the heartbeat"
            )
        return cls(
            url=env.get("RECAP_REDIS_URL", cls.url).strip() or cls.url,
            prefix=env.get("RECAP_REDIS_PREFIX", cls.prefix).strip() or cls.prefix,
            capacity=int(number("RECAP_QUEUE_CAPACITY", cls.capacity, 1)),
            max_attempts=int(number("RECAP_JOB_MAX_ATTEMPTS", cls.max_attempts, 1)),
            heartbeat_seconds=heartbeat,
            heartbeat_ttl_seconds=ttl,
        )


class QueueFull(Exception):
    """The queue holds `capacity` waiting jobs already: try again later."""

    def __init__(self, depth: int, capacity: int) -> None:
        super().__init__(f"the incident queue is full ({depth} of {capacity} jobs waiting)")
        self.depth, self.capacity = depth, capacity


class IdempotencyConflict(Exception):
    """An Idempotency-Key that already named a different request (another scenario or
    configuration): nothing is started, and the earlier submission is left as it was."""

    def __init__(self, earlier: str) -> None:
        super().__init__(f"this Idempotency-Key already named a different request ({earlier})")
        self.earlier = earlier


@dataclass
class Job:
    """One submitted incident and every attempt at it."""

    job_id: str
    run_id: str  # the run of the current (or last) attempt
    scenario_id: str
    config: dict[str, Any]  # the checked RunConfig, as JSON
    status: str = "queued"
    runs: list[str] = field(default_factory=list)  # every attempt's run, oldest first
    attempt: int = 0  # how many times a worker claimed it
    max_attempts: int = 3
    idempotency_key: str = ""
    request_digest: str = ""  # what was asked (scenario and configuration), for the key's check
    worker: str = ""
    submitted_at: str = ""
    claimed_at: str = ""
    finished_at: str = ""
    category: str = ""  # why it ended as it did (or was requeued): see Worker and reconcile
    detail: str = ""
    cancel_requested: bool = False

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES

    def to_hash(self) -> dict[str, str]:
        data = asdict(self)
        data["config"] = json.dumps(self.config, sort_keys=True)
        data["runs"] = json.dumps(self.runs)
        data["cancel_requested"] = "1" if self.cancel_requested else ""
        return {key: str(value) for key, value in data.items()}

    @classmethod
    def from_hash(cls, data: Mapping[str, str]) -> Job | None:
        if not data.get("job_id"):
            return None
        return cls(
            job_id=data["job_id"],
            run_id=data.get("run_id", ""),
            scenario_id=data.get("scenario_id", ""),
            config=json.loads(data.get("config") or "{}"),
            status=data.get("status", "queued"),
            runs=json.loads(data.get("runs") or "[]"),
            attempt=int(data.get("attempt") or 0),
            max_attempts=int(data.get("max_attempts") or 1),
            idempotency_key=data.get("idempotency_key", ""),
            request_digest=data.get("request_digest", ""),
            worker=data.get("worker", ""),
            submitted_at=data.get("submitted_at", ""),
            claimed_at=data.get("claimed_at", ""),
            finished_at=data.get("finished_at", ""),
            category=data.get("category", ""),
            detail=data.get("detail", ""),
            cancel_requested=bool(data.get("cancel_requested")),
        )


class JobQueue:
    """The job queue on one Redis server. Every method is one or a few atomic commands."""

    def __init__(self, config: QueueConfig, redis: Redis | None = None) -> None:
        self.config = config
        self.redis = redis or Redis(config.url)

    def connection(self) -> Redis:
        """A separate connection (a worker's blocking claim loop needs one of its own)."""
        return Redis(self.config.url, timeout=self.redis.timeout)

    # --- keys -------------------------------------------------------------------------------

    def _key(self, *parts: str) -> str:
        return ":".join((self.config.prefix, *parts))

    @property
    def queue_key(self) -> str:
        return self._key("queue")

    def processing_key(self, worker_id: str) -> str:
        return self._key("processing", worker_id)

    # --- submitting -------------------------------------------------------------------------

    def submit(
        self,
        *,
        run_id: str,
        scenario_id: str,
        config: dict[str, Any],
        idempotency_key: str = "",
        request_digest: str = "",
    ) -> tuple[Job, bool]:
        """Queue one incident; (the job, True) when created, (the earlier job, False) when
        the idempotency key was used before for the same request. Raises QueueFull when the
        queue is at capacity, IdempotencyConflict when the key named another request."""
        r = self.redis
        if idempotency_key and not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise ValueError("an Idempotency-Key is 1 to 128 letters, digits, '.', '_', ':' or '-'")
        idem = self._key("idem", idempotency_key) if idempotency_key else ""
        if idem:
            earlier = self._job_named(r.get(idem))
            if earlier is not None:
                return self._same_request(earlier, request_digest), False
        depth = r.llen(self.queue_key)
        if depth >= self.config.capacity:
            raise QueueFull(depth, self.config.capacity)
        job = Job(
            job_id=new_id("job"),
            run_id=run_id,
            scenario_id=scenario_id,
            config=config,
            runs=[run_id],
            max_attempts=self.config.max_attempts,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            submitted_at=utcnow().isoformat(),
        )
        if idem and not r.set(
            idem, job.job_id, nx=True, px=self.config.idempotency_ttl_seconds * 1000
        ):
            earlier = self._job_named(r.get(idem))  # an identical submission won the race
            if earlier is not None:
                return self._same_request(earlier, request_digest), False
            # The key outlived a submission that broke before its job was written: take it.
            r.set(idem, job.job_id, px=self.config.idempotency_ttl_seconds * 1000)
        r.transaction(
            [
                ("HSET", self._key("job", job.job_id), *_flat(job.to_hash())),
                ("SET", self._key("run", run_id), job.job_id),
                ("LPUSH", self.queue_key, job.job_id),
                ("LPUSH", self._key("recent"), job.job_id),
                ("LTRIM", self._key("recent"), 0, 199),
                ("HINCRBY", self._key("stats"), "submitted", 1),
            ]
        )
        return job, True

    # --- reading -----------------------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return Job.from_hash(self.redis.hgetall(self._key("job", job_id)))

    def job_for_run(self, run_id: str) -> Job | None:
        return self._job_named(self.redis.get(self._key("run", run_id)))

    def depth(self) -> int:
        return self.redis.llen(self.queue_key)

    def position(self, job_id: str) -> int | None:
        """1 for the next job to be claimed; None when it is not waiting."""
        waiting = self.redis.lrange(self.queue_key, 0, -1)
        return len(waiting) - waiting.index(job_id) if job_id in waiting else None

    # --- claiming and running ----------------------------------------------------------------

    def claim(
        self, worker_id: str, *, block_seconds: float, redis: Redis | None = None
    ) -> Job | None:
        """Take the oldest waiting job (atomic: two workers never get the same one)."""
        r = redis or self.redis
        processing = self.processing_key(worker_id)
        job_id = r.blmove(self.queue_key, processing, block_seconds)
        if job_id is None:
            return None
        job = self.get(job_id)
        if job is None or job.terminal:  # cancelled while waiting, or its record is gone
            r.lrem(processing, 0, job_id)
            return None
        attempt = r.hincrby(self._key("job", job_id), "attempt", 1)
        claimed = utcnow().isoformat()
        r.hset(
            self._key("job", job_id),
            {"status": "running", "worker": worker_id, "claimed_at": claimed},
        )
        job.attempt, job.status, job.worker, job.claimed_at = attempt, "running", worker_id, claimed
        return job

    def set_run(self, job: Job, run_id: str) -> None:
        """Name the run of this attempt (a retried job runs as a new run)."""
        if run_id not in job.runs:
            job.runs.append(run_id)
        job.run_id = run_id
        self.redis.transaction(
            [
                (
                    "HSET",
                    self._key("job", job.job_id),
                    "run_id",
                    run_id,
                    "runs",
                    json.dumps(job.runs),
                ),
                ("SET", self._key("run", run_id), job.job_id),
            ]
        )

    def finish(
        self, job: Job, worker_id: str, *, status: JobStatus, category: str = "", detail: str = ""
    ) -> None:
        """End a job: out of the worker's list, its status and why."""
        job.status, job.category, job.detail = status, category, detail
        job.finished_at = utcnow().isoformat()
        fields = {
            "status": status,
            "category": category,
            "detail": detail[:500],
            "finished_at": job.finished_at,
        }
        self.redis.transaction(
            [
                ("HSET", self._key("job", job.job_id), *_flat(fields)),
                ("LREM", self.processing_key(worker_id), 0, job.job_id),
                ("HINCRBY", self._key("stats"), status, 1),
            ]
        )

    def requeue(self, job: Job, held_by: str, *, category: str, detail: str) -> None:
        """Put an interrupted job back at the front of the queue (its next attempt runs next)."""
        job.status, job.category, job.detail = "queued", category, detail
        fields = {"status": "queued", "category": category, "detail": detail[:500], "worker": ""}
        self.redis.transaction(
            [
                ("HSET", self._key("job", job.job_id), *_flat(fields)),
                ("LREM", self.processing_key(held_by), 0, job.job_id),
                ("RPUSH", self.queue_key, job.job_id),
                ("HINCRBY", self._key("stats"), "requeued", 1),
            ]
        )

    def unclaim(self, job: Job, worker_id: str) -> None:
        """Give back a job claimed as the worker was stopping: to the front, attempt unused."""
        job.status, job.worker = "queued", ""
        job.attempt -= 1
        self.redis.transaction(
            [
                ("HINCRBY", self._key("job", job.job_id), "attempt", -1),
                ("HSET", self._key("job", job.job_id), "status", "queued", "worker", ""),
                ("LREM", self.processing_key(worker_id), 0, job.job_id),
                ("RPUSH", self.queue_key, job.job_id),
            ]
        )

    # --- cancelling --------------------------------------------------------------------------

    def request_cancel(self, job: Job) -> Literal["cancelled", "requested", "finished"]:
        """Cancel a waiting job at once; ask the worker running one to stop it."""
        if job.terminal:
            return "finished"
        if job.status == "queued" and self.redis.lrem(self.queue_key, 0, job.job_id):
            job.status = "cancelled"
            fields = {
                "status": "cancelled",
                "category": "cancelled_before_start",
                "finished_at": utcnow().isoformat(),
            }
            self.redis.transaction(
                [
                    ("HSET", self._key("job", job.job_id), *_flat(fields)),
                    ("HINCRBY", self._key("stats"), "cancelled", 1),
                ]
            )
            return "cancelled"
        self.redis.hset(self._key("job", job.job_id), {"cancel_requested": "1"})
        job.cancel_requested = True
        return "requested"

    def cancel_requested(self, job_ids: list[str]) -> set[str]:
        if not job_ids:
            return set()
        replies = self.redis.pipeline(
            [("HGET", self._key("job", j), "cancel_requested") for j in job_ids]
        )
        return {j for j, reply in zip(job_ids, replies, strict=True) if reply}

    # --- workers and their heartbeats --------------------------------------------------------

    def register(self, worker_id: str, info: Mapping[str, Any]) -> None:
        r = self.redis
        r.sadd(self._key("workers"), worker_id)
        r.set(self._key("worker", worker_id), json.dumps(dict(info)), px=self._ttl_ms())

    def heartbeat(self, worker_id: str, info: Mapping[str, Any]) -> bool:
        """Refresh this worker's key; False when it had already expired (another process may
        have reconciled its jobs: the worker must stop them, not carry on)."""
        return self.redis.set(
            self._key("worker", worker_id), json.dumps(dict(info)), xx=True, px=self._ttl_ms()
        )

    def unregister(self, worker_id: str) -> None:
        r = self.redis
        r.delete(self._key("worker", worker_id))
        if not r.llen(self.processing_key(worker_id)):
            r.srem(self._key("workers"), worker_id)

    def workers(self) -> dict[str, dict[str, Any] | None]:
        """Every registered worker and its heartbeat (None: expired, so the worker is dead)."""
        found: dict[str, dict[str, Any] | None] = {}
        for worker_id in sorted(self.redis.smembers(self._key("workers"))):
            beat = self.redis.get(self._key("worker", worker_id))
            found[worker_id] = json.loads(beat) if beat else None
        return found

    def held_by(self, worker_id: str) -> list[str]:
        return self.redis.lrange(self.processing_key(worker_id), 0, -1)

    def alive(self, worker_id: str) -> bool:
        """Whether this worker's heartbeat is current."""
        return bool(worker_id) and self.redis.exists(self._key("worker", worker_id))

    def release(self, job_id: str, held_by: str) -> None:
        """Take a job that needs nothing more out of a worker's list."""
        self.redis.lrem(self.processing_key(held_by), 0, job_id)

    def forget(self, worker_id: str) -> None:
        """Drop a dead worker that holds no job any more."""
        if not self.redis.llen(self.processing_key(worker_id)):
            self.redis.srem(self._key("workers"), worker_id)

    def lock(self, job_id: str, owner: str, ttl_seconds: float = 60.0) -> bool:
        return self.redis.set(self._key("lock", job_id), owner, nx=True, px=int(ttl_seconds * 1000))

    def unlock(self, job_id: str) -> None:
        self.redis.delete(self._key("lock", job_id))

    def count(self, name: str, amount: int = 1) -> None:
        self.redis.hincrby(self._key("stats"), name, amount)

    # --- the queue view ----------------------------------------------------------------------

    def snapshot(self, recent: int = 30) -> dict[str, Any]:
        """Depth, capacity, workers with their jobs, the newest jobs and the counters."""
        r = self.redis
        workers = []
        for worker_id, beat in self.workers().items():
            workers.append(
                {
                    "worker_id": worker_id,
                    "alive": beat is not None,
                    "heartbeat": beat,
                    "jobs": self.held_by(worker_id),
                }
            )
        newest = r.lrange(self._key("recent"), 0, recent - 1) if recent > 0 else []
        jobs = [job for job in (self.get(j) for j in newest) if job is not None]
        counters = {key: int(value) for key, value in r.hgetall(self._key("stats")).items()}
        waiting = r.lrange(self.queue_key, 0, -1)
        return {
            "depth": len(waiting),
            "waiting": waiting,
            "capacity": self.config.capacity,
            "workers": workers,
            "jobs": jobs,
            "counters": counters,
        }

    # --- helpers -----------------------------------------------------------------------------

    def _ttl_ms(self) -> int:
        return int(self.config.heartbeat_ttl_seconds * 1000)

    def _job_named(self, job_id: str | None) -> Job | None:
        return self.get(job_id) if job_id else None

    @staticmethod
    def _same_request(earlier: Job, request_digest: str) -> Job:
        if earlier.request_digest and request_digest and earlier.request_digest != request_digest:
            raise IdempotencyConflict(earlier.job_id)
        return earlier


def _flat(mapping: Mapping[str, Any]) -> list[str]:
    return [str(item) for pair in mapping.items() for item in pair]
