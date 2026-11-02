"""The CI workflow file parses and runs the checks the docs promise.

A YAML error in .github/workflows/ci.yml stops GitHub from starting any job, and nothing
local would notice (an unquoted ": " in a step name did exactly that in M4).
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def test_the_ci_workflow_parses_and_runs_every_promised_check() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert set(jobs) == {"python", "dashboard"}
    commands = "\n".join(str(step.get("run", "")) for step in jobs["python"]["steps"])
    for check in (
        "ruff check .",
        "ruff format --check .",
        "mypy",
        "scripts/secret_scan.py",
        "scripts/recordings_manifest.py --check",
        "pytest",
        "scripts/smoke_test.py",
    ):
        assert check in commands, check
    checkout = jobs["python"]["steps"][0]
    assert checkout.get("with", {}).get("fetch-depth") == 0  # the secret scan reads history
    dashboard = "\n".join(str(step.get("run", "")) for step in jobs["dashboard"]["steps"])
    assert "npm test" in dashboard and "npm run build" in dashboard
