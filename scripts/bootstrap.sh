#!/usr/bin/env bash
# One-time local setup: Python virtualenv + dependencies, and the dashboard's npm packages.
# Nothing here contacts a cloud account or a paid API.
set -euo pipefail
cd "$(dirname "$0")/.."

if command -v uv >/dev/null 2>&1; then
  uv venv .venv --python 3.11
  uv pip install --python .venv/bin/python -e ".[dev]"
else
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -e ".[dev]"
fi

if command -v npm >/dev/null 2>&1; then
  (cd apps/dashboard && npm install)
else
  echo "npm not found: skipping the dashboard (the CLI demo and API still work)."
fi

echo
echo "Setup complete. Try:"
echo "  .venv/bin/python scripts/demo.py --scenario bad-deployment"
echo "  .venv/bin/python -m pytest -q"
