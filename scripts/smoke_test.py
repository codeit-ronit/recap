"""Fast end-to-end smoke test: one incident through the HTTP API, real MCP subprocess, simulator.

    python scripts/smoke_test.py

Uses a temporary data directory, so it never touches your local data/ folder.
Exit code 0 means the whole stack (API -> controller -> MCP -> simulator -> oracle) works.
"""

from __future__ import annotations

import secrets
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

from fastapi.testclient import TestClient

from apps.api.auth import OperatorAuth
from apps.api.main import create_app
from recap.core.config import load_settings
from recap.runtime import Runtime


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        runtime = Runtime(load_settings(data_dir=Path(tmp)))
        # A throwaway operator token, sent like the dashboard sends it after sign-in, and
        # requests addressed to localhost (the API refuses other host names).
        token = secrets.token_urlsafe(32)
        app = create_app(runtime, auth=OperatorAuth(token))
        headers = {"Authorization": f"Bearer {token}"}
        with TestClient(app, base_url="http://localhost", headers=headers) as client:
            assert client.get("/health").json()["status"] == "ok"
            run_id = client.post(
                "/incidents/run",
                json={"scenario_id": "bad-deployment", "config": {"approval_mode": "auto"}},
            ).json()["run_id"]
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                body = client.get(f"/runs/{run_id}").json()
                if body["status"] != "running" and body["record"]:
                    break
                time.sleep(0.2)
            state = body["record"]["state"] if body["record"] else body.get("error")
            events = client.get(f"/runs/{run_id}/events").json()
            print(f"run {run_id}: {state} ({len(events)} trace events, simulated environment)")
            return 0 if state == "RESOLVED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
