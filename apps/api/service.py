"""Run manager: runs incidents and evaluations in the background for the API.

The HTTP layer stays thin: it validates requests and calls this manager, which owns
the Runtime, the background asyncio tasks, the live event hub used for server-sent
events (SSE), and the approval channel through which humans approve or reject
proposals from the dashboard.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from evals.runner import (
    LATEST_DIR,
    EvalReport,
    load_eval_configs,
    new_report_dir,
    report_as_dict,
    run_evaluation,
)
from recap.core.ids import new_id
from recap.core.models import TERMINAL_STATES, RunConfig, RunRecord, TraceEvent
from recap.runtime import Runtime
from recap.safety.approvals import ExternalApprovalChannel
from recap.telemetry.sinks import BroadcastHub
from recap.telemetry.tracer import TraceCategory

KEEPALIVE_SECONDS = 15


def sse_frame(event: TraceEvent) -> str:
    """Format one trace event as a server-sent-events frame."""
    return f"id: {event.seq}\nevent: trace\ndata: {event.model_dump_json()}\n\n"


class RunManager:
    """Starts runs as background tasks and streams their trace events."""

    def __init__(self, runtime: Runtime | None = None) -> None:
        self.runtime = runtime or Runtime(hub=BroadcastHub())
        if self.runtime.hub is None:  # live streaming needs a hub on the runtime
            self.runtime.hub = BroadcastHub()
        self.hub = self.runtime.hub
        self.channel = ExternalApprovalChannel()  # humans decide through POST /approvals/...
        self.tasks: dict[str, asyncio.Task[RunRecord]] = {}
        self.errors: dict[str, str] = {}
        self.eval_task: asyncio.Task[EvalReport] | None = None
        self.eval_dir: Path | None = None
        self.eval_log: list[str] = []

    # --- incident runs ---------------------------------------------------------------------

    def start_run(self, scenario_id: str, config: RunConfig) -> str:
        """Start a run in the background and return its id immediately."""
        run_id = new_id("run")
        task = asyncio.create_task(
            self.runtime.run_incident(
                scenario_id, config, approval_channel=self.channel, run_id=run_id
            )
        )
        task.add_done_callback(lambda t, rid=run_id: self._remember_error(rid, t))
        self.tasks[run_id] = task
        return run_id

    def _remember_error(self, run_id: str, task: asyncio.Task[RunRecord]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.errors[run_id] = f"{type(task.exception()).__name__}: {task.exception()}"

    def status(self, run_id: str) -> str:
        """Where a run stands, as far as this server process can tell.

        A run this server started is 'running', 'finished' or 'error'. A run started by
        another process (the CLI, or an earlier server) is 'finished' once its stored state
        is terminal and 'external' until then: its live events exist only in that process.
        'unknown' means there is no such run.
        """
        task = self.tasks.get(run_id)
        if task is None:
            record = self.runtime.runs.get(run_id)
            if record is None:
                return "unknown"
            return "finished" if record.state in TERMINAL_STATES else "external"
        if not task.done():
            return "running"
        return "error" if run_id in self.errors else "finished"

    async def stream(self, run_id: str, after_seq: int = 0) -> AsyncIterator[str]:
        """Replay stored events; for a run this server is running, then forward live ones.

        Any other run has no live events in this process, so its stream ends after the replay.
        """
        queue = self.hub.subscribe(run_id)  # subscribe first so nothing is missed
        # Decided before the replay: if the run ends in between, its last events are then
        # either in the replay or already waiting in the queue.
        live = self.status(run_id) == "running"
        last = after_seq
        try:
            for event in self.runtime.events.list_events(run_id, after_seq):
                last = event.seq
                yield sse_frame(event)
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
                yield sse_frame(event)
                if event.category == TraceCategory.RUN_SUMMARY:
                    return
        finally:
            self.hub.unsubscribe(run_id, queue)

    # --- evaluations ------------------------------------------------------------------------

    def start_evaluation(self, config_names: list[str] | None) -> dict[str, Any]:
        """Start the evaluation protocol in the background (one at a time).

        Raises ConfigError for an unknown configuration name before anything starts, so the
        client gets an immediate error instead of a failed evaluation later.
        """
        load_eval_configs(config_names)
        if self.eval_task is not None and not self.eval_task.done():
            return {"status": "already running", "report_dir": str(self.eval_dir)}
        self.eval_dir = new_report_dir()
        self.eval_log = []
        self.eval_task = asyncio.create_task(
            run_evaluation(self.eval_dir, config_names=config_names, progress=self.eval_log.append)
        )
        return {"status": "started", "report_dir": str(self.eval_dir)}

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
