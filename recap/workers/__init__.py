"""Durable background execution of incidents (M6 W3): an optional Redis-backed job queue and
the worker processes that run queued incidents through the same harness as the API.

    resp.py       a minimal Redis client (the RESP2 protocol; no extra dependency)
    queue.py      jobs: idempotent submission, atomic claims, heartbeats, cancellation, stats
    reconcile.py  what an interrupted run did, from the persisted evidence, and how it is closed
    worker.py     the worker: bounded concurrency, heartbeats, graceful shutdown, recovery
    faults.py     deterministic crash injection at real execution boundaries (tests only)

The run records, traces and approval log in SQLite stay the source of truth: Redis only
coordinates who runs what, and never decides whether an incident or a fix succeeded.
"""
