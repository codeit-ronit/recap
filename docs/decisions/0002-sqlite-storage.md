# ADR 0002: Store everything in local SQLite files

**In one sentence:** runs, evidence, trace events, approvals, experience memory and the
simulated world all live in plain SQLite files under `data/`, and the safety-critical
"use once" and "decide once" rules are single SQL statements.

## Status

Accepted.

## Context

V1 has to persist several kinds of state:

- run checkpoints and the evidence each run collected, so the API and dashboard can show them;
- trace events, so a run can be replayed and streamed;
- approval requests and decisions, which must be **atomic**: an approval may be used once
  and a request decided once, even if two processes race;
- experience memory that survives across runs;
- the simulated world, which two processes must see at the same time: the MCP tool server
  (for the tools) and the controller (for the recovery oracle).

The setup has to be zero-effort on a laptop, and the data has to be easy to inspect.

## Decision

Use SQLite through one small helper, `connect` in
[recap/core/db.py](../../recap/core/db.py). It turns on WAL mode (write-ahead logging, which
lets one process read while another writes), sets `synchronous=NORMAL` and a 10-second busy
timeout.

| File (under `data/`) | What it holds | Written by |
|---|---|---|
| `recap.db` | `runs` and `evidence` (`RunStore`, [recap/harness/store.py](../../recap/harness/store.py)), `events` (`SqliteTraceSink`, [recap/telemetry/sinks.py](../../recap/telemetry/sinks.py)), `approval_requests` and `approvals` (`ApprovalStore`, [recap/safety/approvals.py](../../recap/safety/approvals.py)) | controller, API |
| `memory.db` | `episodes` (`ExperienceStore`, [recap/memory/store.py](../../recap/memory/store.py)) | controller, when a run ends |
| `sandbox/<run_id>.db` | the simulated world of one run, as one JSON row (`SandboxStateStore`, [recap/environments/simulated/state_store.py](../../recap/environments/simulated/state_store.py)) | the simulator, in both processes |
| `traces/<run_id>.jsonl` | the same trace events as JSON lines (`JsonlTraceSink`); a plain file, not SQLite | tracer |

Rules that matter for safety are written as SQL, so the database enforces them:

- **Single use:** `UPDATE approvals SET consumed_at = ? WHERE approval_id = ? AND consumed_at IS NULL`
  must change exactly one row (`ApprovalStore.consume`).
- **Decide once:** a decision moves a request out of `pending` with
  `UPDATE approval_requests SET status = ? WHERE proposal_id = ? AND status = ?` (the last
  value is `pending`), and `proposal_id` is `UNIQUE` in the `approvals` table.
- **No lost updates in the simulator:** each write runs inside `BEGIN IMMEDIATE`, which takes
  the write lock before reading.

The location is `data/` by default and can be changed with `RECAP_DATA_DIR`. Every
evaluation writes to its own fresh data directory (`run_evaluation` in
[evals/runner.py](../../evals/runner.py)). All of these files are local runtime data and are
listed in `.gitignore`.

## Consequences

### Good

- Nothing to install or run: no database server, no account, no network.
- Every file opens in any SQLite browser, so a reviewer can inspect exactly what a run stored.
- Single-use approvals hold even across processes.
  `tests/unit/test_gateway.py::test_approval_used_by_another_process_first_is_denied`
  simulates another process consuming the approval first.
- The MCP server subprocess and the oracle share one simulated world without any extra
  service.
- Evaluations cannot see demo data, because they use a different directory.

### Bad

- **The files are trusted state.** Anyone who can write `recap.db` can insert an approval
  row, and anyone who can write `memory.db` can plant a "verified success". RECAP's checks
  run when data passes through its own stores, not on the files. There is no encryption and
  no signing. See [the threat model](../threat-model.md#residual-risks-and-limitations).
- One machine, one writer at a time. That is fine for one user, not for a shared service.
- No migration tool. Tables are created with `CREATE TABLE IF NOT EXISTS`, and records carry a
  schema version string (`recap.v1`), but a schema change needs a hand-written migration.
- Memory search scores every readable episode in Python. That is fine for hundreds of
  episodes, not millions.
- Redaction is best-effort pattern matching. Tool outputs are redacted before they become
  evidence (so checkpoint fields built from evidence inherit it), and run errors and trace
  payloads are redacted too, but a secret in a format the patterns miss is stored as observed.

## Alternatives considered

- **PostgreSQL or another database server.** It would add access control and scale, but it
  needs a server to install and run. The store classes are small, so they could be ported
  later.
- **In-memory state only.** Nothing would survive a restart or be inspectable, and the MCP
  server subprocess could not share the simulated world with the oracle.
- **Plain JSON files.** No transactions, so two deciders could both "use" one approval.
- **A vector database for memory.** Not needed yet: V1 memory uses deterministic word
  overlap, which is simple and fast enough for hundreds of episodes, and the similarity
  function can be replaced later without changing the store.
