# ADR 0033: Published real-cluster runs: an allowlisted, pinned, read-only source behind the same views

**In one sentence:** M5's real Kind and SREGym runs open in Mission Control on a fresh checkout,
offline, because the API reads their committed exports through one more read-only source: an
allowlist that pins every file's SHA-256, RECAP's own schemas and cross-file checks, and a
status, `published`, that every write path refuses.

## Status

Accepted (M6, W2; built 2026-11-08). Builds on [ADR 0027](0027-a-typed-read-only-api-over-the-run-records.md)
(the typed run views), [ADR 0029](0029-one-evaluation-record-per-run-and-modes-that-never-share-a-number.md)
(evaluation records and the benchmark's own verdict) and [ADR 0032](0032-a-real-kubernetes-target-behind-the-same-interface.md)
(the real-cluster runs themselves). The work is logged in [m6-progress.md](../m6-progress.md).

## Context

M5 exported its real-cluster runs to `evals/live/` (run record, trace, model recording, README)
and their evaluation records to `evals/reports/batches/`. A fresh checkout could read the files,
but Mission Control could not open them: the run views read only the server's own data folder,
and the clusters and the model that produced the runs live on a VM that is usually stopped. An
interviewer should be able to inspect the real investigation, approval and recovery, and SREGym's
grader verdict, without AWS, a cluster or a model.

Importing other people's files into a safety-critical tool is risky in its own right: a path can
point anywhere, a file can be altered, a schema can drift, a file can hold a secret, and a
historical approval must never become a permission.

## Decision

1. **An explicit allowlist.** `evals/live/MANIFEST.json` (schema `recap-published-runs-v1`) names
   each published run: its folder (a plain name inside `evals/live/`), its run id, the real
   cluster it worked on, its evaluation batch, and the SHA-256 and size of each of its four files.
   The title and notes are a person's, checked in review; every other fact comes from the files.
2. **Checked before shown, refused whole** (`evals/published.py`). A run is shown only when its
   folder and files are real (no links, nothing outside `evals/live/`, size caps), each file's bytes
   match the pins (read once, hashed, then those bytes parsed, as JSON only), the record and every
   trace event validate against `RunRecord` and `TraceEvent`, and the files agree with each other
   and with the run's evaluation record: one run id, the declared real target, the run's own
   recording, every approval in the trace, the same final state and oracle verdict. Any text
   shaped like a credential refuses the run, and the reason never repeats it. A refused run is
   not shown at all; `GET /published` names it with the reason.
3. **The same views, one more source.** The API's reader falls back to a published run when its
   own store does not know the id, with status `published`. `GET /runs/{id}`, its timeline,
   evidence, retrieval, usage, events and stream serve it; `GET /runs` stays this server's own runs.
   The dashboard adds Published Runs, a historical banner on every run page, and a group in the run
   picker; it builds no second trace view.
4. **Read-only, by construction.** A published run is never written to a store, followed as a live
   run or replayed (replays come only from `fixtures/recordings/`). Cancel answers 409. Its
   approvals were never loaded into the approval store, so they cannot be decided again or reused.
5. **What was not exported is said, not guessed.** The evidence registry's contents and the
   original server's decision log were not exported, so a cited evidence id is not a finding there,
   and the views say what is not recorded. The eight real-cluster runs whose traces were not
   published (failures and blocks included) are listed from their evaluation records only.
6. **The benchmark's verdict stays apart.** SREGym's grader verdict (`benchmark_verdict`, recorded
   by hand) is shown next to RECAP's oracle, never merged with it, and the SREGym run's notes say it
   is not a held-out benchmark result.

## Consequences

### Good

- A fresh checkout opens real Kubernetes evidence offline, in the views a reviewer already knows.
- Tampered, incompatible or unsafe artifacts are refused with a reason, and tests prove each case.
- Adding a published run is a review step: export it, add its manifest entry and prose, and
  `python scripts/published_runs.py --check`.

### Bad

- The published traces lack their evidence contents, so Evidence & Decisions shows ids and
  verdicts, not the tool outputs; the model recording holds what the model was shown.
- Re-pinning after a deliberate change to an export is a manual step (`--update`), and its prose
  needs a person's review.

## Alternatives considered

- **Copy the runs into the server's data folder on start.** Rejected: imported content would
  reach the stores that write paths use, and a historical approval could look like a live one.
- **A separate viewer for exports.** Rejected: a second trace engine to keep in step with the
  real one, and a different experience from every other run.
- **Load every folder in `evals/live/`.** Rejected: only an explicit, reviewed allowlist may
  decide what a public dashboard shows.
