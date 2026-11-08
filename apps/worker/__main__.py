"""Run incidents from the Redis job queue (M6).

    RECAP_EXECUTION_BACKEND=redis RECAP_REDIS_URL=redis://127.0.0.1:6379/0 python -m apps.worker
    python -m apps.worker --check     # container health: the last heartbeat is recent

The worker reads the same configuration as the API (configs/, RECAP_DATA_DIR, RECAP_TARGET,
the provider settings) and writes to the same data folder, so the API serves what it runs and
a person approves its fixes through the API. SIGTERM or SIGINT stops it gracefully
(recap/workers/worker.py). Exit codes: 0 stopped, 2 misconfigured, 1 unhealthy (--check).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.worker", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 0 when RECAP_WORKER_HEALTH_FILE was touched by a heartbeat recently, else 1",
    )
    args = parser.parse_args(argv)
    if args.check:
        return _check()

    logging.basicConfig(
        level=os.environ.get("RECAP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Imported here so --check stays fast (it runs every few seconds in a container).
    from recap.core.errors import ConfigError
    from recap.runtime import Runtime
    from recap.workers.faults import FaultPlan
    from recap.workers.queue import JobQueue, QueueConfig
    from recap.workers.worker import Worker, WorkerSettings

    try:
        config = QueueConfig.from_environment()
        faults = FaultPlan.from_environment()
    except ConfigError as exc:
        print(f"worker: {exc}", file=sys.stderr)
        return 2
    if config is None:
        print(
            "worker: set RECAP_EXECUTION_BACKEND=redis (and RECAP_REDIS_URL): with the default "
            "in-process backend the API runs incidents itself and needs no worker",
            file=sys.stderr,
        )
        return 2
    worker = Worker(
        Runtime(),
        JobQueue(config),
        WorkerSettings.from_environment(),
        faults=faults,
        admit=admission,
    )
    asyncio.run(_serve(worker))
    return 0


def admission(runtime: Any, scenario_id: str, config: Any) -> Any:
    """Check a job exactly as the API checks a request (apps/api/service.py): a job that reached
    Redis some other way runs only what POST /incidents/run would have started."""
    from apps.api.service import RunRefused, check_run_request
    from recap.providers.local_memory import check_run_memory

    checked = check_run_request(runtime, scenario_id, config)
    refusal = check_run_memory(runtime.settings, checked.provider)
    if refusal:
        raise RunRefused(503, refusal)
    return checked


async def _serve(worker: object) -> None:
    from recap.workers.worker import Worker

    assert isinstance(worker, Worker)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await worker.run(stop)


def _check() -> int:
    """Healthy when a heartbeat Redis accepted touched the health file within 3 heartbeats."""
    path = os.environ.get("RECAP_WORKER_HEALTH_FILE", "").strip()
    if not path:
        print("worker --check: RECAP_WORKER_HEALTH_FILE is not set", file=sys.stderr)
        return 1
    beat = float(os.environ.get("RECAP_WORKER_HEARTBEAT_SECONDS", "") or 5)
    try:
        age = time.time() - Path(path).stat().st_mtime
    except OSError:
        print("worker --check: no heartbeat yet", file=sys.stderr)
        return 1
    if age > 3 * beat + 5:
        print(f"worker --check: last heartbeat {age:.0f} s ago", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
