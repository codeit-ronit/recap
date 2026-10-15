# ADR 0008: Pin exact package versions with a constraints file

**In one sentence:** `pyproject.toml` keeps loose version ranges, and a separate
`constraints.txt` pins every Python package to the exact version the test suite last passed
with, so a fresh install, CI and `scripts/bootstrap.sh` all get the same environment.

## Status

Accepted (M0, 2026-10-15).

## Context

V1 declared ranges such as `fastapi>=0.115` in [pyproject.toml](../../pyproject.toml). A
fresh install months later can pull a newer release of any of the 50 packages in the
environment and break the suite for reasons that have nothing to do with RECAP's code. The
next phase changes safety-critical code (approvals, the MCP write boundary, the verifier),
so a test result has to mean the same thing on every machine. The dashboard was already
pinned: `package.json` uses exact versions and `package-lock.json` is committed.

## Decision

- [constraints.txt](../../constraints.txt) at the repository root lists `name==version` for
  every installed package of the environment that passed the M0 baseline (781 passed,
  1 skipped). It does not add packages; it only fixes the versions of the ones that the
  `pyproject.toml` extras ask for.
- Every install uses it: `pip install -c constraints.txt -e ".[dev]"`. CI
  ([.github/workflows/ci.yml](../../.github/workflows/ci.yml)) and
  [scripts/bootstrap.sh](../../scripts/bootstrap.sh) do this.
- [.python-version](../../.python-version) (`3.11`) and
  [apps/dashboard/.nvmrc](../../apps/dashboard/.nvmrc) (`24`) pin the interpreters; CI reads
  the Node version from `.nvmrc`.
- CI also runs `ruff format --check .`, so formatting drift fails the build.
- An upgrade is a deliberate change: upgrade, run the full suite, regenerate the file with
  the command in its header, and commit both together.

## Consequences

### Good

- The same versions everywhere, so a red test points at RECAP's code.
- `pyproject.toml` stays readable and keeps honest minimum versions for people who install
  RECAP as a library.
- No new tool is needed: pip and uv both understand `-c`.

### Bad

- Security fixes in dependencies arrive only when someone upgrades on purpose.
- The file can drift from `pyproject.toml` if a dependency is added without regenerating it;
  the install then picks the newest version of the new package. Regenerating after every
  dependency change is a manual rule.
- Hashes are not pinned, so the file protects against version drift, not against a
  tampered package index.

## Alternatives considered

- **A lock tool such as `uv lock` or `pip-tools` with hashes.** Stronger (hash pinning) but
  adds a tool and a second dependency file format to learn. Worth revisiting before any
  shared deployment.
- **Exact pins in `pyproject.toml`.** Mixes "what RECAP needs" with "what we tested", and
  makes RECAP hard to install next to other packages.
