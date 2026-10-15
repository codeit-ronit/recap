# `apps/dashboard` — RECAP V1 dashboard

A dark, minimal React + TypeScript (Vite) single-page app for demonstrating RECAP: live incident
runs, the agent timeline, evidence and RAG citations, experience memory, safety and approvals,
evaluations and the architecture.

**Everything on screen comes from the RECAP API** (`apps/api/main.py`). The dashboard has no
hardcoded metrics, sample data or fake runs. If there are no runs yet, the pages say so. The top
bar of every page says which environment is plugged in, read from `GET /system`: an amber
**SIMULATED LOCAL ENVIRONMENT · V1** badge for the deterministic local simulator (the V1
default; no real system is changed), or a red **LIVE ENVIRONMENT (&lt;kind&gt;) · V1** badge when a
live adapter is configured. Until the API answers, the badge is muted.

## Run it

Requirements: Node 18+ and npm (tested with Node 24). All dependencies are pinned in `package.json`.
`npm test` needs Node 22.18+, which runs the TypeScript tests directly (no test packages).

### Development (hot reload)

```bash
# terminal 1 — from the repository root
uvicorn apps.api.main:app --reload            # API on http://127.0.0.1:8000

# terminal 2
cd apps/dashboard
npm install
npm run dev                                    # http://localhost:5173
```

The dev server proxies `/api/*` to `http://127.0.0.1:8000` and strips the `/api` prefix, so
`/api/runs` becomes `GET /runs`.

### Served by FastAPI (one process)

```bash
cd apps/dashboard && npm install && npm run build   # writes apps/dashboard/dist
cd ../.. && uvicorn apps.api.main:app               # http://127.0.0.1:8000/
```

The API mounts `apps/dashboard/dist` at `/` when that folder exists at startup, so (re)start
uvicorn after the first build. In this mode the API is on the same origin, at the root.

`VITE_API_URL` (build- or dev-time environment variable) overrides the API origin, e.g.
`VITE_API_URL=http://127.0.0.1:8000 npm run dev`. The API's CORS settings allow
`localhost:5173`.

Scripts: `npm run dev` · `npm run build` (`tsc --noEmit && vite build`, strict TypeScript) ·
`npm run preview` · `npm test` (Node's built-in test runner on `tests/*.test.ts`: the honest
labels in `src/format.ts`, such as the environment badge and the cost line).

## Pages

Navigation is hash-based, so every view is a shareable deep link:
`#/timeline?run=<run_id>`, `#/evidence?run=<run_id>&ev=<evidence_id>`. The selected run follows
you between the run-scoped pages (Timeline, Evidence, Memory, Safety). Without `?run=` they show
the latest run.

| Page | What it shows | API |
|---|---|---|
| **Overview** | The implementation behind each interface (environment adapter, provider, retriever, router, policy, oracle, transport, memory size); run counts by final state; the latest evaluation headline (labeled *synthetic benchmark · simulated · small N*); recent runs (click a run to open its timeline). | `/system`, `/runs`, `/evals/results` |
| **Live Incident Run** | Scenario picker (demo scenarios first; ground truth only behind *Show ground truth*). Config: strategy, RAG, memory, approval mode (manual = you approve here, auto = simulation auto-approver), simulated tool latency. **Run incident** starts a run and streams it: a lifecycle stepper, a readable live event feed (click a line for the raw event), an approval card with the exact proposal, and a result panel (diagnosis, oracle verdict, approval, episode, estimated usage, and the backend's own cost line from `run.summary`). | `POST /incidents/run`, `/runs/{id}/stream` (SSE), `/approvals/pending`, `POST /approvals/{proposal_id}`, `/runs/{id}` |
| **Agent Timeline** | A Gantt-style chart: one lane per agent plus the controller. Bars are MCP tool calls (`tool.call` paired with `tool.result` by `invocation_id`). Shaded spans show when each agent was active; dots are controller decisions, denials and injection flags. A bracket marks the measured window where parallel specialists ran concurrently (`overlap_ms`). Long human approval waits can be compressed into an axis break. Includes a table view, the routing decision per round (mode, rule, reason, signals, step allocation) and the orchestration summary (the overlap is measured; how long the agents would take back to back is labeled an estimate). | `/runs/{id}/events` |
| **Evidence & RAG** | The evidence registry (id, tool, source ref, agent, trust level, flags such as `prompt_injection_suspected`, token estimate; expand a row for the raw content); hypothesis checks with passed/failed rules; retrieved runbook chunks as citations, with the reason each retrieval ran or was skipped; a knowledge search box. | `/runs/{id}`, `/runs/{id}/events`, `/knowledge/search` |
| **Experience Memory** | All episodes (outcome, oracle-verified, partition, successful and unsuccessful actions, `simulated` provenance; expand a row for evidence summaries and provenance). For a run: its memory hits with similarity, mechanism match and **safe to reuse YES/NO** with the reason. *Similar ≠ safe to reuse.* | `/memory`, `/runs/{id}` |
| **Safety & Approvals** | Pending approvals (approve or reject them here, signed in); the append-only approval decision log; for a run: mitigation policy verdicts with rule ids (the last id is the deciding rule), every gateway decision in the trace, proposals (declared vs trusted risk), approvals (single use, consumed), mitigation attempts (denied ones in red) and prompt-injection flags with the flagged excerpt. Also a static legend of the policy rule ids (documentation, not metrics). | `/approvals/pending`, `/runs/{id}`, `/runs/{id}/events` |
| **Evaluations** | **Run evaluation** starts the protocol in the background and shows its progress; if it fails, the page shows the error. Results: the test-suite table per configuration, the red-team table, per-case outcomes (filterable), the caveats and reproducibility metadata. Labeled *Synthetic benchmark on the simulator · small N · no statistical claims · not SREGym*. | `POST /evals/run`, `/evals/results` |
| **Architecture** | A layered HTML/CSS diagram of the RECAP flow with trust boundaries (dashed amber = untrusted data, solid teal = trusted code), next to the live wiring from `/system`. Swapping the infrastructure means writing one `TargetEnvironment` adapter and changing `configs/environment.yaml`: the controller, gateway, RAG, memory, router and orchestrator code stay unchanged. Still simulator-specific (data, not code): the log conventions the scripted policy and verifier parse, the policy allowlists and the runbooks. | `/system` |

## How live updates work

- The page first asks `GET /runs/{id}` for the run's status. A run that another process started
  (the CLI, an earlier server) and that has not finished is `external`: its live events exist
  only in that process, so the page loads its recorded events once and says so instead of
  waiting for updates that cannot arrive.
- A run's events come from `GET /runs/{id}/stream` (server-sent events named `trace`, one
  `TraceEvent` JSON per message). The stream replays stored events first, so opening or reloading
  `#/live?run=<id>` catches up on the run. The client closes the stream when `run.summary`
  arrives.
- If the stream errors, the page falls back to polling `GET /runs/{id}/events?after_seq=` every
  second and checks the run status now and then. Events are de-duplicated by `seq`.
- **Sign in to act.** Starting a run, approving or rejecting, cancelling a run and starting an
  evaluation need the operator token that the API prints at start-up (also in
  `data/operator-token`). The **Sign in** box (top right) checks the token with
  `GET /auth/session` before saving it, keeps it in the tab's `sessionStorage` (never a cookie;
  `src/auth.ts`) and sends it as `Authorization: Bearer …`. Signed out, the approval buttons are
  disabled with a hint; a 401 shows the same hint. Read-only views and the live stream need no
  token.
- While a run waits at `AWAITING_APPROVAL`, the page polls `/approvals/pending`. Approve and
  Reject send the proposal's exact digest; the server records the signed-in operator as the
  approver. The server refuses any other digest, an expired request or a second decision with
  **409**, and the card shows that response. A rejection also revokes an approval that was not
  used yet. *Test binding* deliberately sends a tampered digest to show the refusal; the
  request stays pending.
- A live run has a **Cancel run** button (`POST /runs/{id}/cancel`); its pending approval
  request expires with it. The Safety page shows the append-only approval decision log
  (`/approvals/events`, and per proposal of the selected run).
- If the API is down, the shell shows the backend start command once and every view waits
  instead of failing.

## Code layout

```
src/
  main.tsx, App.tsx      entry point; layout, sidebar, top bar (environment badge, provider, API status)
  api.ts                 typed fetch helpers, ApiError (offline / HTTP status / FastAPI detail), SSE helper
  auth.ts                the operator token (sessionStorage, memory fallback) and the Authorization header
  types.ts               TypeScript mirror of recap/core/models.py, API responses and the eval report
  hooks.ts               useHashRoute, usePolling, useRunEvents (SSE + polling fallback), useRunSelection
  format.ts              state/verdict colors, number and time formatting, honest labels (environment
                         badge, provider, cost), describeEvent (event → one line)
  styles.css             the whole theme (CSS variables, no framework)
  components/            Badge/Chip, Card (+ StatTile, Meter, Notice, Segmented, Toggle), Table,
                         StateStepper, EventFeed, ApprovalCard, OperatorSignIn, Timeline (Gantt), JsonView,
                         RunPicker (run scope bar), EmptyState (+ error, loading, error boundary), Icon
  pages/                 one file per page
tests/                   Node tests of the pure helpers in src/format.ts and src/auth.ts (npm test)
```

Event payloads are read defensively: every payload key is optional in `types.ts`, so a missing
or renamed key degrades one line of text instead of breaking a page. The categories and keys
used are the ones the controller, harness and gateway emit (see `recap/telemetry/tracer.py`
`TraceCategory` and the `emit(...)` calls in `recap/harness/`).
