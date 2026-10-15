"""Trace sinks: where TraceEvents go after the Tracer emits them.

The Tracer (tracer.py) numbers and redacts each event, then calls `write(event)` on every
sink of the run (see `Runtime.trace_sinks` in recap/runtime.py):

    JsonlTraceSink   data/traces/<run_id>.jsonl, one JSON object per line. Easy to read,
                     grep, diff and attach to a bug report.
    SqliteTraceSink  the `events` table in data/recap.db. The API pages through it
                     (GET /runs/{id}/events?after_seq=N).
    BroadcastSink    pushes events into a BroadcastHub, which feeds the dashboard's live
                     stream (GET /runs/{id}/stream, server-sent events).

Everything is local: no collector and no hosted service. A sink that raises is logged by
the Tracer and never breaks an incident run.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
from pathlib import Path

from recap.core.db import connect
from recap.core.models import TraceEvent

LIVE_QUEUE_SIZE = 1000  # events buffered per live subscriber before new ones are dropped

# Run IDs become file names, so only plain ID characters are allowed (no "/" and no "..").
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    category TEXT NOT NULL,
    stage TEXT NOT NULL,
    ts TEXT NOT NULL,
    agent TEXT,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events (run_id, seq);
"""


class JsonlTraceSink:
    """Appends every event of a run as one JSON line to <directory>/<run_id>.jsonl."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def path_for(self, run_id: str) -> Path:
        """The JSONL file that holds the trace of `run_id`."""
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError(f"run_id is not safe to use as a file name: {run_id!r}")
        return self.directory / f"{run_id}.jsonl"

    def write(self, event: TraceEvent) -> None:
        """Append one event; the directory and the file are created on first use."""
        path = self.path_for(event.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")


class SqliteTraceSink:
    """Stores events in SQLite so the API can page through a run's trace by sequence number."""

    def __init__(self, db_path: Path | str) -> None:
        self._conn = connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # Runs write from the event loop while sync API endpoints read from worker
        # threads, so one lock keeps the shared connection consistent.
        self._lock = threading.Lock()

    def write(self, event: TraceEvent) -> None:
        """Insert one event (a repeated event_id is ignored: events never change)."""
        row = (
            event.event_id,
            event.run_id,
            event.seq,
            event.category,
            event.stage,
            event.ts.isoformat(),
            event.agent,
            event.model_dump_json(),
        )
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO events"
                " (event_id, run_id, seq, category, stage, ts, agent, json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            self._conn.commit()  # commit per event so readers see it right away

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 5000) -> list[TraceEvent]:
        """Events of one run with seq > after_seq, oldest first (after_seq lets clients resume)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT json FROM events WHERE run_id = ? AND seq > ?"
                " ORDER BY seq, event_id LIMIT ?",
                (run_id, after_seq, limit),
            ).fetchall()
        return [TraceEvent.model_validate_json(row["json"]) for row in rows]


class BroadcastHub:
    """In-process publish/subscribe of live events, keyed by run ID (feeds the SSE stream).

    Everything runs on one asyncio event loop, so no locks are needed. A subscriber whose
    queue is full misses new live events instead of slowing the run down; it can catch up
    from `SqliteTraceSink.list_events(run_id, after_seq)`.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[TraceEvent]]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue[TraceEvent]:
        """Start receiving the live events of one run."""
        queue: asyncio.Queue[TraceEvent] = asyncio.Queue(maxsize=LIVE_QUEUE_SIZE)
        self._subscribers.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[TraceEvent]) -> None:
        """Stop delivering to `queue` (calling it twice is harmless)."""
        queues = self._subscribers.get(run_id, [])
        if queue in queues:
            queues.remove(queue)
        if not queues:
            self._subscribers.pop(run_id, None)  # forget runs nobody watches any more

    def publish(self, event: TraceEvent) -> None:
        """Hand an event to every subscriber of its run, without ever waiting."""
        for queue in self._subscribers.get(event.run_id, []):
            # A stalled dashboard tab must never stall the incident run.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)


class BroadcastSink:
    """Trace sink that forwards every event to a BroadcastHub."""

    def __init__(self, hub: BroadcastHub) -> None:
        self.hub = hub

    def write(self, event: TraceEvent) -> None:
        """Publish the event to the live subscribers of its run."""
        self.hub.publish(event)
