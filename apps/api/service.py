"""Run manager: runs incidents and evaluations in the background for the API.

The HTTP layer stays thin: it validates requests and calls this module.

    RunManager          owns the Runtime, the background asyncio tasks, the live event hub
                        used for server-sent events (SSE), and the approval channel through
                        which humans approve or reject proposals from the dashboard; with
                        RECAP_EXECUTION_BACKEND=redis (M6) it queues runs for workers instead
                        and follows their stored traces
    check_run_request   decides what a client may start (scenario, safety, the per-run
                        provider choice, a replay's committed recording)
    RunReader           reads what runs persisted (checkpoints, stored traces, evidence),
                        the committed recordings and the evaluation batches, and hands them
                        to apps/api/views.py, which builds the typed responses (ADR 0027)

Nothing here writes a run's records: the controller does. The reader only reads them.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Collection, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from apps.api.dto import (
    API_SCHEMA,
    BatchSource,
    EnvironmentCheck,
    EnvironmentCheckItem,
    ExecutionBackend,
    ExportProvenance,
    FixtureIntegrity,
    JobView,
    QueueView,
    RunDetail,
    RunStarted,
    RunStatus,
    RunSummary,
    TimelineFilters,
)
from apps.api.views import (
    FixtureCheck,
    LocalPaths,
    RunFilters,
    batch_view,
    environment_run_label,
    evals_view,
    evidence_view,
    job_view,
    origin_labels,
    provider_view,
    published_provenance,
    published_view,
    queue_view,
    replays_view,
    retrieval_view,
    run_summary,
    system_view,
    timeline_view,
    usage_view,
)
from evals.published import MANIFEST as PUBLISHED_MANIFEST
from evals.published import PublishedIndex, PublishedRun, load_published
from evals.records import EvalBatch, find_batches
from evals.runner import (
    LATEST_DIR,
    EvalReport,
    load_eval_configs,
    new_report_dir,
    report_as_dict,
    run_evaluation,
)
from recap.core.config import REPO_ROOT, Settings
from recap.core.errors import ConfigError
from recap.core.ids import canonical_json, new_id, sha256_hex, utcnow
from recap.core.models import TERMINAL_STATES, Evidence, RunConfig, RunRecord, TraceEvent
from recap.providers.recordings import MANIFEST, FixtureInfo, list_fixtures, sha256_of
from recap.runtime import Runtime
from recap.safety.approvals import ExternalApprovalChannel
from recap.telemetry.sinks import BroadcastHub
from recap.telemetry.tracer import TraceCategory
from recap.workers.queue import IDEMPOTENCY_KEY, IdempotencyConflict, Job, JobQueue, QueueConfig
from recap.workers.resp import Redis, RedisError

KEEPALIVE_SECONDS = 15
POLL_SECONDS = 0.5  # how often the stream of a worker's run reads its stored trace (M6)
REDIS_TIMEOUT_SECONDS = 2.0  # the API's Redis calls: a request never waits long for the queue
REDIS_BACKOFF_SECONDS = 2.0  # after Redis did not answer, reads skip it this long
RUN_SCAN_LIMIT = 10_000  # GET /runs filters and counts at most this many of the newest runs
EVENT_PAGE = 5_000  # trace events read from SQLite per query
ENVIRONMENT_CHECK_SECONDS = 20.0  # POST /environment/check: the preflight's whole time


def sse_frame(event: TraceEvent, paths: LocalPaths | None = None) -> str:
    """Format one trace event as a server-sent-events frame. Its `id` is the event's seq, so
    a reconnecting client's Last-Event-ID says exactly where to resume. With `paths`, local
    folders in the event are replaced, as in every other response."""
    if paths is None:
        data = event.model_dump_json()
    else:
        scrubbed = paths.scrub(event.model_dump(mode="json"))
        data = json.dumps(scrubbed, separators=(",", ":"), ensure_ascii=False)
    return f"id: {event.seq}\nevent: trace\ndata: {data}\n\n"


def request_digest(scenario_id: str, config: RunConfig) -> str:
    """What a submission asked for, for an Idempotency-Key's check."""
    asked = config.model_dump(mode="json", exclude_unset=True)  # what the worker will run
    return sha256_hex(canonical_json({"scenario_id": scenario_id, "config": asked}))


@dataclass(frozen=True)
class Submission:
    """What POST /incidents/run did: started a run here, or queued it for a worker (M6)."""

    run_id: str
    status: Literal["started", "queued"]
    job: Job | None = None
    position: int | None = None  # 1 = claimed next
    duplicate: bool = False  # the Idempotency-Key named an earlier submission: nothing new


class RunManager:
    """Starts runs as background tasks and streams their trace events.

    With RECAP_EXECUTION_BACKEND=redis (M6), runs are queued for workers instead
    (recap/workers/): the run's job says where it stands, and its stream follows the trace the
    worker stores. Nothing connects to Redis until a request needs it, so the API starts, and
    serves every read, without Redis; only what needs the queue answers 503 then.
    """

    def __init__(self, runtime: Runtime | None = None, *, queue: JobQueue | None = None) -> None:
        self.runtime = runtime or Runtime(hub=BroadcastHub())
        if self.runtime.hub is None:  # live streaming needs a hub on the runtime
            self.runtime.hub = BroadcastHub()
        settings = self.runtime.settings
        # Local folders a response must never show (the data folder, the checkout, home).
        self.paths = LocalPaths.of(settings.paths.root, settings.paths.data_dir)
        self.hub = self.runtime.hub
        self.channel = ExternalApprovalChannel()  # humans decide through POST /approvals/...
        self.tasks: dict[str, asyncio.Task[RunRecord]] = {}
        self.errors: dict[str, str] = {}
        self.eval_task: asyncio.Task[EvalReport] | None = None
        self.eval_dir: Path | None = None
        self.eval_log: list[str] = []
        if queue is None:
            config = QueueConfig.from_environment()
            if config is not None:
                queue = JobQueue(config, Redis(config.url, timeout=REDIS_TIMEOUT_SECONDS))
        self.queue = queue
        self._idempotent: dict[str, tuple[str, str]] = {}  # in-process: key -> (run, request)
        self._queue_down_until = 0.0

    @property
    def backend(self) -> ExecutionBackend:
        return "inprocess" if self.queue is None else "redis"

    # --- the configured target -------------------------------------------------------------

    async def environment_check(self) -> EnvironmentCheck:
        """Reach the configured target the way a run's reset would first reach it, and change
        nothing: a real cluster's adapter runs its own preflight (each kubeconfig's server and
        CA fingerprint, the version skew, the monitoring clock), bounded in time."""
        settings = self.runtime.settings
        environment = self.runtime.environment_factory(settings.environment, "readiness-check")
        info = environment.describe()
        if info.is_simulated:
            item = EnvironmentCheckItem(
                name="simulated environment", ok=True, detail="local and in-process"
            )
        else:
            preflight = getattr(environment, "preflight", None)
            if preflight is None:
                item = EnvironmentCheckItem(
                    name="preflight", ok=False, detail="this adapter has no preflight to run"
                )
            else:
                try:
                    async with asyncio.timeout(ENVIRONMENT_CHECK_SECONDS):
                        await preflight()
                    item = EnvironmentCheckItem(
                        name="preflight",
                        ok=True,
                        detail="credentials pinned to this cluster; API server and versions "
                        "answer as expected",
                    )
                except TimeoutError:
                    detail = f"no answer within {ENVIRONMENT_CHECK_SECONDS:g} s (tunnel down?)"
                    item = EnvironmentCheckItem(name="preflight", ok=False, detail=detail)
                except Exception as exc:  # any refusal is the answer, said as it is
                    item = EnvironmentCheckItem(name="preflight", ok=False, detail=str(exc)[:300])
        return EnvironmentCheck(
            checked_at=utcnow(),
            environment_kind=info.kind,
            simulated=info.is_simulated,
            ok=item.ok,
            checks=[item],
        )

    # --- incident runs ---------------------------------------------------------------------

    def submit(
        self, scenario_id: str, config: RunConfig, *, idempotency_key: str = ""
    ) -> Submission:
        """Start one incident run here, or queue it for a worker (the Redis backend).

        An Idempotency-Key names one submission: the same key with the same request returns
        that submission again and starts nothing; with another request it raises
        IdempotencyConflict. Only the Redis backend keeps keys across a restart (a day). The
        Redis backend raises QueueFull when the queue is at capacity and RedisError when Redis
        does not answer (nothing was queued, or a retry with the same key finds it).
        """
        if idempotency_key and not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise ValueError("an Idempotency-Key is 1 to 128 letters, digits, '.', '_', ':' or '-'")
        digest = request_digest(scenario_id, config)
        if self.queue is None:
            earlier = self._idempotent.get(idempotency_key) if idempotency_key else None
            if earlier is not None:
                if earlier[1] != digest:
                    raise IdempotencyConflict(earlier[0])
                return Submission(earlier[0], "started", duplicate=True)
            run_id = self.start_run(scenario_id, config)
            if idempotency_key:
                self._idempotent[idempotency_key] = (run_id, digest)
            return Submission(run_id, "started")
        job, created = self.queue.submit(
            run_id=new_id("run"),
            scenario_id=scenario_id,
            # Only the fields the request set: the worker's RunConfig then has the same
            # model_fields_set, so scenario overrides apply exactly as in-process.
            config=config.model_dump(mode="json", exclude_unset=True),
            idempotency_key=idempotency_key,
            request_digest=digest,
        )
        position = self.queue.position(job.job_id) if job.status == "queued" else None
        return Submission(job.run_id, "queued", job=job, position=position, duplicate=not created)

    def start_run(self, scenario_id: str, config: RunConfig) -> str:
        """Start a run in the background and return its id immediately."""
        run_id = new_id("run")
        task = asyncio.create_task(
            self.runtime.run_incident(
                scenario_id, config, approval_channel=self.channel, run_id=run_id
            )
        )
        task.add_done_callback(partial(self._remember_error, run_id))
        self.tasks[run_id] = task
        return run_id

    def cancel(self, run_id: str) -> str | None:
        """Cancel a run: "cancelling" (it stops at its next step), "cancelled" (a queued run,
        taken off the queue: nothing ran), or None when there is nothing to cancel.

        The controller ends a cancelled run as FAILED ("run cancelled") and expires a pending
        approval request, so nobody can approve it afterwards. A worker's run is cancelled at
        the worker's next heartbeat. Raises RunRefused for a run that was retried as another
        one, and RedisError when Redis does not answer.
        """
        task = self.tasks.get(run_id)
        if task is not None:
            if task.done():
                return None
            task.cancel()
            return "cancelling"
        if self.queue is None:
            return None
        job = self.queue.job_for_run(run_id)
        if job is None:
            return None
        if job.run_id != run_id:
            raise RunRefused(
                409,
                f"run '{run_id}' was interrupted and runs again as '{job.run_id}': cancel that run",
            )
        outcome = self.queue.request_cancel(job)
        if outcome == "finished":
            return None
        return "cancelled" if outcome == "cancelled" else "cancelling"

    def _remember_error(self, run_id: str, task: asyncio.Task[RunRecord]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.errors[run_id] = f"{type(task.exception()).__name__}: {task.exception()}"

    def status(self, run_id: str) -> RunStatus:
        """Where a run stands, as far as this server process can tell.

        A run this server started is 'running', 'finished' or 'error'. A run started by
        another process (the CLI, or an earlier server) is 'finished' once its stored state
        is terminal and 'external' until then: its live events exist only in that process.
        'unknown' means there is no such run. With the Redis backend, a run that is not
        finished takes its status from its job: 'queued', 'running' (a worker runs it; its
        stream follows the stored trace), 'interrupted' (its worker stopped and nobody has
        settled it yet), 'cancelled' (taken off the queue) or 'error' (it failed to start).
        """
        record = None if run_id in self.tasks else self.runtime.runs.get(run_id)
        return self.status_for(run_id, record)

    def status_for(self, run_id: str, record: RunRecord | None) -> RunStatus:
        """status(), for a caller that has already read the run's record (None: no record)."""
        task = self.tasks.get(run_id)
        if task is None:
            if record is not None and record.state in TERMINAL_STATES:
                return "finished"
            job = self._job(run_id)
            if job is not None and job.run_id == run_id:
                if job.status == "queued":
                    return "queued"
                if job.status == "running":
                    return "running" if self._worker_alive(job.worker) else "interrupted"
                if record is None:
                    return "cancelled" if job.status == "cancelled" else "error"
            return "unknown" if record is None else "external"
        if not task.done():
            return "running"
        return "error" if run_id in self.errors else "finished"

    # --- the job queue (M6) -------------------------------------------------------------------

    def _job(self, run_id: str) -> Job | None:
        """The job of a run; None without the Redis backend, or while Redis does not answer."""
        if self.queue is None or time.monotonic() < self._queue_down_until:
            return None
        try:
            return self.queue.job_for_run(run_id)
        except RedisError:
            self._queue_down_until = time.monotonic() + REDIS_BACKOFF_SECONDS
            return None

    def _worker_alive(self, worker_id: str) -> bool:
        if self.queue is None:
            return False
        try:
            return self.queue.alive(worker_id)
        except RedisError:
            return True  # unknown: never call a run interrupted on a guess

    def job_for(self, run_id: str) -> JobView | None:
        """The run's job (GET /runs/{id}); None without one, or while Redis does not answer."""
        job = self._job(run_id)
        return self._job_view(job) if job is not None else None

    def job(self, job_id: str) -> JobView | None:
        """GET /jobs/{id}. Raises RedisError when Redis does not answer."""
        if self.queue is None:
            return None
        found = self.queue.get(job_id)
        return self._job_view(found) if found is not None else None

    def _job_view(self, job: Job) -> JobView | None:
        assert self.queue is not None
        try:
            position = self.queue.position(job.job_id) if job.status == "queued" else None
            alive = self.queue.alive(job.worker) if job.status == "running" else None
        except RedisError:
            return None
        return job_view(job, position=position, worker_alive=alive)

    def queue_view(self) -> QueueView:
        """GET /queue: how runs execute and, with Redis, the queue, its workers and jobs."""
        running = sum(1 for task in self.tasks.values() if not task.done())
        if self.queue is None:
            return queue_view("inprocess", running=running)
        try:
            snapshot = self.queue.snapshot()
        except RedisError as exc:
            error = f"Redis did not answer: {exc}"
            return queue_view("redis", running=running, config=self.queue.config, error=error)
        return queue_view("redis", running=running, config=self.queue.config, snapshot=snapshot)

    def queue_reachable(self) -> bool | None:
        """Whether Redis answers a PING (None: the in-process backend has no queue)."""
        if self.queue is None:
            return None
        try:
            return self.queue.redis.ping()
        except RedisError:
            return False

    def _job_may_continue(self, run_id: str) -> bool:
        """Whether a worker may still add events to this run's trace."""
        if self.queue is None:
            return False
        try:
            job = self.queue.job_for_run(run_id)
        except RedisError:  # unknown: keep following unless the run has ended
            record = self.runtime.runs.get(run_id)
            return record is None or record.state not in TERMINAL_STATES
        return job is not None and job.run_id == run_id and not job.terminal

    async def stream(self, run_id: str, after_seq: int = 0) -> AsyncIterator[str]:
        """Replay stored events after `after_seq`; for a run this server is running, then
        forward live ones. Each event is sent once: a live event already replayed is skipped.

        Any other run has no live events in this process, so its stream ends after the replay;
        with the Redis backend, a run a worker runs (or will run) is followed from its stored
        trace instead (_follow_stored).
        """
        if self.queue is not None and run_id not in self.tasks:
            async for frame in self._follow_stored(run_id, after_seq):
                yield frame
            return
        queue = self.hub.subscribe(run_id)  # subscribe first so nothing is missed
        # Decided before the replay: if the run ends in between, its last events are then
        # either in the replay or already waiting in the queue.
        live = self.status(run_id) == "running"
        last = after_seq
        try:
            for event in self.runtime.events.list_events(run_id, after_seq):
                last = event.seq
                yield sse_frame(event, self.paths)
                if event.category == TraceCategory.RUN_SUMMARY:
                    return
            if not live:
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    if self.status(run_id) != "running":
                        return
                    yield ": keep-alive\n\n"
                    continue
                if event.seq <= last:
                    continue
                last = event.seq
                yield sse_frame(event, self.paths)
                if event.category == TraceCategory.RUN_SUMMARY:
                    return
        finally:
            self.hub.unsubscribe(run_id, queue)

    async def _follow_stored(self, run_id: str, after_seq: int) -> AsyncIterator[str]:
        """Stream a worker's run from SQLite: stored events after `after_seq`, then each new one
        as the worker stores it, in seq order, until its run.summary (written by the worker,
        or by whoever closed the run after its worker stopped). The trace is persisted before
        it is streamed, so a worker that stops loses no event, and a client that reconnects
        with Last-Event-ID resumes right after the last event it received."""
        last = after_seq
        quiet = 0.0
        while True:
            events = await asyncio.to_thread(
                self.runtime.events.list_events, run_id, last, EVENT_PAGE
            )
            for event in events:
                last = event.seq
                yield sse_frame(event, self.paths)
                if event.category == TraceCategory.RUN_SUMMARY:
                    return
            if events:
                quiet = 0.0
                continue
            if not await asyncio.to_thread(self._job_may_continue, run_id):
                # Events stored between the read above and that check are still sent.
                for event in await asyncio.to_thread(
                    self.runtime.events.list_events, run_id, last, EVENT_PAGE
                ):
                    yield sse_frame(event, self.paths)
                return
            await asyncio.sleep(POLL_SECONDS)
            quiet += POLL_SECONDS
            if quiet >= KEEPALIVE_SECONDS:
                quiet = 0.0
                yield ": keep-alive\n\n"

    # --- evaluations ------------------------------------------------------------------------

    def start_evaluation(self, config_names: list[str] | None) -> dict[str, Any]:
        """Start the evaluation protocol in the background (one at a time).

        Raises ConfigError for an unknown configuration name before anything starts, so the
        client gets an immediate error instead of a failed evaluation later.
        """
        load_eval_configs(config_names)
        if self.eval_task is not None and not self.eval_task.done():
            return {"status": "already running", "report_dir": shown_folder(self.eval_dir)}
        self.eval_dir = new_report_dir()
        self.eval_log = []
        self.eval_task = asyncio.create_task(
            run_evaluation(self.eval_dir, config_names=config_names, progress=self.eval_log.append)
        )
        return {"status": "started", "report_dir": shown_folder(self.eval_dir)}

    def evaluation_results(self) -> dict[str, Any]:
        """The evaluation this server started: running, failed (with its error) or its report.

        Only before this server starts one is the committed evals/reports/latest shown, so a
        failed evaluation is never hidden behind an older report.
        """
        task = self.eval_task
        progress = self.eval_log[-5:]
        if task is None:
            latest = report_as_dict(LATEST_DIR) if (LATEST_DIR / "results.json").exists() else None
            return {"running": False, "progress": progress, "report": latest, "error": None}
        if not task.done():
            return {"running": True, "progress": progress, "report": None, "error": None}
        report: dict[str, Any] | None = None
        error: str | None = None
        if task.cancelled():  # checked first: exception() raises on a cancelled task
            error = "the evaluation was cancelled"
        elif (exc := task.exception()) is not None:
            error = f"{type(exc).__name__}: {exc}"
        else:
            report = task.result().model_dump(mode="json")
        return {"running": False, "progress": progress, "report": report, "error": error}


def shown_folder(folder: Path | None, root: Path = REPO_ROOT) -> str | None:
    """A folder as a response may show it: repo-relative, or only its name (never a local
    path)."""
    if folder is None:
        return None
    return str(folder.relative_to(root)) if folder.is_relative_to(root) else folder.name


# ======================================================================================
# What a client may start
# ======================================================================================


class RunRefused(Exception):
    """A run request the API will not start; `status` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def selection_rule(settings: Settings, name: str, *, launchable_fixtures: int) -> tuple[bool, str]:
    """May a client pick provider `name` for one run, and why (in plain words)?

    The server's default is always allowed (the owner chose it). Otherwise: a paid provider
    never, the scripted baseline always, a replay only with a committed recording, and a
    model provider only if the owner marked it `api_selectable` in configs/providers.yaml.
    A client can therefore never switch on a paid API, nor start real inference or spend a
    free quota the owner did not allow from the API.
    """
    spec = settings.providers.providers.get(name)
    if spec is None:
        return False, "not a configured provider"
    if name == settings.providers.default:
        return True, "the server's default provider"
    if spec.origin == "paid_llm":
        return False, "a paid API: never selectable from the API (this project's budget is $0)"
    if spec.origin == "scripted":
        return True, "the scripted baseline (not an LLM): always selectable, offline, $0"
    if spec.kind == "replay":
        if launchable_fixtures:
            return True, "only with replay_fixture, the id of a committed recording (GET /replays)"
        return False, "no committed recording can be replayed here (GET /replays says why)"
    if spec.api_selectable:
        return True, "marked api_selectable in configs/providers.yaml by the owner"
    return False, "chosen in server configuration only (api_selectable is off for it)"


def check_fixture(
    info: FixtureInfo, folder: Path, scenarios: Collection[str], prompt_version: str
) -> FixtureCheck:
    """Whether a committed recording can be replayed by this server, and why not.

    Its bytes must match the SHA-256 the manifest pins (an altered recording is never
    replayed), it must have been recorded with the prompts this server renders (else its
    requests cannot match), and this server's environment must have its scenario.
    """
    path = folder / info.file
    reasons: list[str] = []
    integrity: FixtureIntegrity
    if Path(info.file).name != info.file or not path.is_file():
        integrity = "missing"
        reasons.append(f"its file {Path(info.file).name} is missing")
    elif sha256_of(path) != info.sha256:
        integrity = "altered"
        reasons.append("its bytes do not match the SHA-256 the manifest pins (altered)")
    else:
        integrity = "intact"
    prompt_ok = info.prompt_version == prompt_version
    if not prompt_ok:
        reasons.append(
            f"it was recorded with {info.prompt_version}, this server renders {prompt_version}: "
            "its requests would not match"
        )
    scenario_ok = info.scenario in scenarios
    if not scenario_ok:
        reasons.append(f"this server's environment has no scenario '{info.scenario}'")
    return FixtureCheck(integrity, prompt_ok, scenario_ok, tuple(reasons))


def check_run_request(
    runtime: Runtime, scenario_id: str, config: RunConfig, *, manifest: Path = MANIFEST
) -> RunConfig:
    """The run config a client may start, or RunRefused (404 or 400) saying why not.

    A replay of a committed recording takes its routing, memory and retrieval from the
    recording's manifest entry (the original run's), so it asks exactly what was recorded;
    a request that sets one of them differently is refused. A client never names a path:
    a recording is chosen by its manifest id.
    """
    from recap.providers.protocol import PROMPT_VERSION

    scenarios = {s.id for s in runtime.list_scenarios()}
    if scenario_id not in scenarios:
        raise RunRefused(404, f"unknown scenario '{scenario_id}'")
    if not config.safety_enabled:
        raise RunRefused(400, "safety cannot be disabled from the API; use the eval runner")
    if config.approval_mode == "auto" and not runtime.environment_info().is_simulated:
        raise RunRefused(
            400,
            "on a live target a person approves every write: the simulation auto-approver "
            "is refused (approval_mode 'manual')",
        )
    settings = runtime.settings
    default = settings.providers.default
    fixtures, problem = _manifest(manifest)
    if config.provider is not None and config.provider != default:
        launchable = sum(
            1
            for info in fixtures
            if check_fixture(info, manifest.parent, scenarios, PROMPT_VERSION).launchable
        )
        allowed, rule = selection_rule(settings, config.provider, launchable_fixtures=launchable)
        if not allowed:
            raise RunRefused(400, f"provider '{config.provider}' cannot be chosen per run: {rule}")
    spec = settings.providers.providers[config.provider or default]
    if spec.kind != "replay":
        if config.replay_fixture is not None:
            raise RunRefused(400, "replay_fixture is only for the replay provider")
        if config.replay_pace:
            raise RunRefused(400, "replay_pace is only for the replay provider")
        return config
    if config.replay_fixture is None:
        if config.provider is not None and config.provider != default:
            raise RunRefused(400, "a replay needs replay_fixture: a committed recording's id")
        return config  # the server's own replay configuration, as before
    if problem is not None:
        raise RunRefused(400, f"the recordings manifest cannot be read: {problem}")
    info = next((f for f in fixtures if f.id == config.replay_fixture), None)
    if info is None:
        raise RunRefused(
            400, f"unknown replay fixture '{config.replay_fixture}' (GET /replays lists them)"
        )
    check = check_fixture(info, manifest.parent, scenarios, PROMPT_VERSION)
    if not check.launchable:
        raise RunRefused(400, f"replay fixture '{info.id}' cannot run: {'; '.join(check.reasons)}")
    if scenario_id != info.scenario:
        raise RunRefused(
            400,
            f"replay fixture '{info.id}' replays scenario '{info.scenario}', not '{scenario_id}'",
        )
    recorded = {
        "strategy": info.strategy,
        "use_memory": info.use_memory,
        "retrieval": info.retrieval,
    }
    for field, value in recorded.items():
        chosen = getattr(config, field)
        if field in config.model_fields_set and chosen is not None and chosen != value:
            raise RunRefused(
                400,
                f"replay fixture '{info.id}' was recorded with {field}={value!r}; "
                "a replay must ask exactly what the original run asked",
            )
    try:
        RunConfig.model_validate(recorded)  # the manifest's values must be valid run settings
    except ValidationError as exc:
        raise RunRefused(
            400, f"replay fixture '{info.id}' has invalid run settings: {exc}"
        ) from None
    # model_copy keeps which fields the client set, so a scenario's own overrides still apply.
    return config.model_copy(update=recorded)


def _manifest(manifest: Path) -> tuple[list[FixtureInfo], str | None]:
    """The committed recordings, or the reason the manifest cannot be read."""
    try:
        return list_fixtures(manifest), None
    except (ConfigError, ValidationError, ValueError, OSError) as exc:
        return [], str(exc).splitlines()[0][:300]


# ======================================================================================
# Reading what runs persisted
# ======================================================================================


@dataclass
class _ExportCache:
    stamp: tuple[tuple[str, int], ...] = ()
    index: dict[str, ExportProvenance] | None = None


@dataclass
class _PublishedCache:
    stamp: tuple[tuple[str, int, int], ...] = ()
    index: PublishedIndex | None = None


class RunReader:
    """Reads persisted runs, traces, evidence, recordings and evaluation batches, and turns
    them into the API's typed views. Read-only: it never changes a record."""

    def __init__(
        self,
        manager: RunManager,
        *,
        manifest: Path = MANIFEST,
        published_manifest: Path = PUBLISHED_MANIFEST,
    ) -> None:
        self.manager = manager
        self.runtime = manager.runtime
        settings = self.runtime.settings
        root = settings.paths.root
        self.paths = manager.paths
        self.manifest = manifest
        self.exports_dir = root / "evals" / "live"  # exported runs (scripts/export_run_trace.py)
        # Published batches are in git; local ones stay in this machine's data folder.
        # A scripted evaluation started with POST /evals/run writes its batch next to its report
        # (evals/reports/runs/<id>/, git-ignored): a local batch too.
        self.batch_roots: dict[BatchSource, list[Path]] = {
            "published": [root / "evals" / "reports" / "batches"],
            "local": [
                settings.paths.data_dir / "evals" / "batches",
                root / "evals" / "reports" / "runs",
            ],
        }
        self._exports = _ExportCache()
        # Real-cluster runs published with the repository (evals/live/MANIFEST.json): read
        # only, checked byte for byte, never written anywhere (evals/published.py).
        self.published_manifest = published_manifest
        self._published = _PublishedCache()

    def json(self, model: BaseModel) -> Any:
        """A response body: the model as JSON data, with every local folder removed."""
        return self.paths.scrub(model.model_dump(mode="json"))

    # --- runs -----------------------------------------------------------------------------

    def runs(self, filters: RunFilters, offset: int, limit: int) -> tuple[list[Any], int]:
        """One page of run summaries (newest first) and how many runs match in all."""
        matching = [r for r in self.runtime.runs.list(RUN_SCAN_LIMIT) if filters.match(r)]
        fixtures, exports = self.fixtures(), self.exports()
        page = [
            self.json(
                run_summary(
                    record,
                    self.manager.status_for(record.run_id, record),
                    fixtures=fixtures,
                    exports=exports,
                )
            )
            for record in matching[offset : offset + limit]
        ]
        return page, len(matching)  # this server's own runs only: published ones are listed apart

    def run_detail(self, run_id: str) -> Any | None:
        """GET /runs/{id}; None for an unknown run."""
        found = self._load(run_id)
        if found is None:
            return None
        record, status = found
        summary = None
        evidence: list[Evidence] = []
        if record is not None:
            summary = self._summary(record, status)
            # A published run's registry contents were not exported (PUBLISHED_LIMITATIONS).
            evidence = [] if status == "published" else self.runtime.runs.evidence(run_id)
        job = self.manager.job_for(run_id) if status != "published" else None
        error = self.manager.errors.get(run_id)
        if error is None and status == "error" and job is not None:
            error = job.detail  # a worker's run that failed before it recorded anything
        detail = RunDetail(
            status=status,
            summary=summary,
            record=record,
            evidence=evidence,
            error=error,
            job=job,
        )
        return self.json(detail)

    def timeline(
        self, run_id: str, *, after_seq: int, limit: int, filters: TimelineFilters
    ) -> Any | None:
        found = self._load(run_id)
        if found is None:
            return None
        record, status = found
        view = timeline_view(
            run_id,
            record,
            self.events(run_id),
            status,
            after_seq=after_seq,
            limit=limit,
            filters=filters,
            local_model_providers=self.local_model_providers(),
        )
        return self.json(view)

    def evidence(self, run_id: str) -> Any | None:
        found = self._load(run_id)
        if found is None:
            return None
        record, status = found
        if status == "published":
            view = evidence_view(
                run_id, record, [], self.events(run_id), status, registry_recorded=False
            )
            return self.json(view)
        stored = self.runtime.runs.evidence(run_id)
        return self.json(evidence_view(run_id, record, stored, self.events(run_id), status))

    def retrieval(self, run_id: str) -> Any | None:
        found = self._load(run_id)
        if found is None:
            return None
        record, status = found
        return self.json(retrieval_view(run_id, record, self.events(run_id), status))

    def usage(self, run_id: str) -> Any | None:
        found = self._load(run_id)
        if found is None:
            return None
        record, status = found
        view = usage_view(
            run_id,
            record,
            self.events(run_id),
            status,
            providers=self.runtime.settings.providers.providers,
            local_model_providers=self.local_model_providers(),
        )
        return self.json(view)

    def run_started(
        self,
        run_id: str,
        scenario_id: str,
        config: RunConfig,
        *,
        submission: Submission | None = None,
    ) -> Any:
        """The 202 answer of POST /incidents/run: what the new run will run with."""
        settings = self.runtime.settings
        name = config.provider or settings.providers.default
        spec = settings.providers.providers[name]
        committed = config.replay_fixture is not None  # checked against the manifest already
        target = self.runtime.environment_info()
        labels = [environment_run_label(target.is_simulated, target.kind)]
        started = RunStarted(
            run_id=run_id,
            scenario_id=scenario_id,
            provider=name,
            provider_class=spec.origin,
            replay_fixture=config.replay_fixture,
            labels=labels + origin_labels(spec.origin, committed_fixture=committed),
        )
        if submission is not None:
            started.status = submission.status
            started.job_id = submission.job.job_id if submission.job is not None else None
            started.queue_position = submission.position
            started.duplicate = submission.duplicate
        return self.json(started)

    def events(self, run_id: str) -> list[TraceEvent]:
        """Every stored event of a run, oldest first, read a page at a time (a published run's
        from its checked trace.jsonl)."""
        published = self.published_run(run_id)
        if published is not None:
            return list(published.events)
        found: list[TraceEvent] = []
        after = 0
        while True:
            page = self.runtime.events.list_events(run_id, after, EVENT_PAGE)
            found += page
            if len(page) < EVENT_PAGE:
                return found
            after = page[-1].seq

    def events_after(self, run_id: str, after_seq: int) -> list[TraceEvent]:
        """GET /runs/{id}/events: the events after `after_seq`, stored or published."""
        published = self.published_run(run_id)
        if published is not None:
            return [event for event in published.events if event.seq > after_seq]
        return self.runtime.events.list_events(run_id, after_seq)

    def _load(self, run_id: str) -> tuple[RunRecord | None, RunStatus] | None:
        """A run's checkpoint and status; None when neither this server, its store nor the
        published runs know it. A run this server has just started may not have a checkpoint
        yet. This server's own copy of a run wins over a published one."""
        record = self.runtime.runs.get(run_id)
        status = self.manager.status_for(run_id, record)
        if record is None and status == "unknown":
            published = self.published_index().get(run_id)
            return (published.record, "published") if published is not None else None
        return record, status

    def _summary(self, record: RunRecord, status: RunStatus) -> RunSummary:
        """A run's summary; a published run's carries where its checked files are."""
        published = None
        if status == "published":
            run = self.published_index().get(record.run_id)
            if run is not None:
                folder = self.published_manifest.parent / run.entry.folder
                shown = shown_folder(folder, self.runtime.settings.paths.root) or run.entry.folder
                published = published_provenance(run, shown)
        return run_summary(
            record, status, fixtures=self.fixtures(), exports=self.exports(), published=published
        )

    # --- published real-cluster runs (evals/published.py) ------------------------------

    def published_index(self) -> PublishedIndex:
        """The published runs as checked: loaded when first asked, and again whenever the
        manifest, an export file or a published batch changed."""
        stamp = self._published_stamp()
        if self._published.index is None or stamp != self._published.stamp:
            published_batches = [b for b, source in self.batches() if source == "published"]
            index = load_published(self.published_manifest, batches=published_batches)
            self._published = _PublishedCache(stamp, index)
        assert self._published.index is not None
        return self._published.index

    def published_run(self, run_id: str) -> PublishedRun | None:
        """A published run this server does not hold itself (its own copy wins), else None."""
        if run_id in self.manager.tasks or self.runtime.runs.get(run_id) is not None:
            return None
        return self.published_index().get(run_id)

    def published(self) -> Any:
        """GET /published."""
        index = self.published_index()
        summaries = [self._summary(run.record, "published") for run in index.runs]
        return self.json(published_view(index, summaries))

    async def published_stream(self, run: PublishedRun, after_seq: int) -> AsyncIterator[str]:
        """A published run's events as server-sent events, then the end: nothing is live."""
        for event in run.events:
            if event.seq > after_seq:
                yield sse_frame(event, self.paths)

    def _published_stamp(self) -> tuple[tuple[str, int, int], ...]:
        live = self.published_manifest.parent
        root = self.runtime.settings.paths.root
        paths = [
            self.published_manifest,
            *sorted(live.glob("*/*")),
            *sorted((root / "evals" / "reports" / "batches").glob("*/batch.json")),
        ]
        stamp = []
        for path in paths:
            try:
                info = path.stat()
            except OSError:
                continue
            stamp.append((str(path), info.st_mtime_ns, info.st_size))
        return tuple(stamp)

    # --- providers, system, replays -----------------------------------------------------

    def providers(self, catalog: Sequence[dict[str, Any]]) -> list[Any]:
        """GET /providers: the catalog's entries, typed, with the per-run choice rules."""
        settings = self.runtime.settings
        launchable = self.launchable_fixtures()
        views = []
        for entry in catalog:
            name = str(entry["name"])
            selectable, rule = selection_rule(settings, name, launchable_fixtures=launchable)
            spec = settings.providers.providers.get(name)
            views.append(self.json(provider_view(entry, spec, selectable, rule)))
        return views

    def system(self) -> Any:
        settings = self.runtime.settings
        launchable = self.launchable_fixtures()
        per_run = [
            name
            for name in settings.providers.providers
            if selection_rule(settings, name, launchable_fixtures=launchable)[0]
        ]
        view = system_view(
            self.runtime.system_summary(),
            replay_fixtures=len(self.fixtures()),
            launchable_fixtures=launchable,
            per_run_providers=per_run,
            api_schema=API_SCHEMA,
        )
        return self.json(view)

    def replays(self) -> Any:
        from recap.providers.protocol import PROMPT_VERSION

        checked, problem = self.fixture_checks()
        view = replays_view(checked, current_prompt_version=PROMPT_VERSION, manifest_error=problem)
        return self.json(view)

    def fixtures(self) -> list[FixtureInfo]:
        """The committed recordings (none when the manifest cannot be read)."""
        return _manifest(self.manifest)[0]

    def fixture_checks(self) -> tuple[list[tuple[FixtureInfo, FixtureCheck]], str | None]:
        from recap.providers.protocol import PROMPT_VERSION

        fixtures, problem = _manifest(self.manifest)
        scenarios = {s.id for s in self.runtime.list_scenarios()}
        folder = self.manifest.parent
        checked = [(f, check_fixture(f, folder, scenarios, PROMPT_VERSION)) for f in fixtures]
        return checked, problem

    def launchable_fixtures(self) -> int:
        return sum(1 for _, check in self.fixture_checks()[0] if check.launchable)

    def local_model_providers(self) -> set[str]:
        """Configured providers served by a local Ollama server (one request at a time)."""
        providers = self.runtime.settings.providers.providers
        return {name for name, spec in providers.items() if spec.api == "ollama"}

    def exports(self) -> dict[str, ExportProvenance]:
        """Exported runs by run id (evals/live/<folder>/run.json), re-read only when changed."""
        files = sorted(self.exports_dir.glob("*/run.json")) if self.exports_dir.is_dir() else []
        stamp = tuple((str(path), path.stat().st_mtime_ns) for path in files)
        if self._exports.index is not None and stamp == self._exports.stamp:
            return self._exports.index
        root = self.runtime.settings.paths.root
        index: dict[str, ExportProvenance] = {}
        for path in files:
            try:
                run_id = json.loads(path.read_text(encoding="utf-8")).get("run_id")
            except (OSError, ValueError, AttributeError):
                continue  # a half-written export is skipped, never guessed
            if isinstance(run_id, str):
                folder = path.parent
                index[run_id] = ExportProvenance(
                    folder=shown_folder(folder, root) or folder.name,
                    files=sorted(p.name for p in folder.iterdir() if p.is_file()),
                )
        self._exports = _ExportCache(stamp, index)
        return index

    # --- evaluations ------------------------------------------------------------------------

    def batches(self) -> list[tuple[EvalBatch, BatchSource]]:
        """Every evaluation batch (evals/records.py), newest first, with where it is kept."""
        found: dict[str, tuple[EvalBatch, BatchSource]] = {}
        for source, roots in self.batch_roots.items():  # published first: it wins a tie
            for batch in find_batches(roots):
                found.setdefault(batch.batch_id, (batch, source))
        return sorted(found.values(), key=lambda pair: pair[0].created_at, reverse=True)

    def evals(self) -> Any:
        return self.json(evals_view(self.batches()))

    def batch(self, batch_id: str) -> Any | None:
        """One batch by id; published batches win over a local one of the same id."""
        order: dict[BatchSource, int] = {"published": 0, "local": 1}
        matches = sorted(
            (pair for pair in self.batches() if pair[0].batch_id == batch_id),
            key=lambda pair: order[pair[1]],
        )
        return self.json(batch_view(*matches[0])) if matches else None
