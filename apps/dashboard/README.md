# `apps/dashboard` — RECAP Mission Control

**RECAP Mission Control** is a dark, restrained React + TypeScript (Vite) single-page app for
demonstrating RECAP: the incident lab, the multi-agent trace, evidence and decisions, the runbook
knowledge base, experience memory, safety and approvals, evaluations and the architecture.

**Everything on screen comes from the RECAP API** (`apps/api/main.py`). The dashboard has no
hardcoded metrics, sample data or fake runs. If there are no runs yet, the pages say so. RECAP
runs against a **simulated** environment (a simulated shop); nothing on screen suggests a
production system. A strip across the top of every page says which environment is plugged in,
from the server only: **SIMULATED ENVIRONMENT** (amber) when `GET /system` reports
`environment.is_simulated`, a red **LIVE ENVIRONMENT · &lt;kind&gt;** if a live adapter were ever
configured, the label `GET /health` sends until `/system` answers, and **ENVIRONMENT NOT
CONFIRMED** before either does (never a guess).

## Run it

Requirements: Node 18+ and npm (tested with Node 24). All dependencies are pinned in `package.json`.
`npm test` needs Node 22.18+, which runs the TypeScript tests directly (no test packages; see
[Testing](#testing)).

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
`npm run preview` · `npm test` (`scripts/test.mjs`: the pure helpers in `tests/*.test.ts` and the
React component tests in `tests/*.test.tsx`, in one `node --test` run; see [Testing](#testing)).

## The shell: navigation, environment and data source

`src/components/Shell.tsx` (`AppShell`) frames every page; `src/App.tsx` only fetches what it
shows (`GET /health`, `/system`, `/approvals/pending`, and `GET /runs/{id}` for a selected run).

- **Environment strip** (fixed at the top, every page, every width): `EnvironmentBanner`, see
  above, with the API status on its right (`API online · v<version>` from `GET /health`,
  `API offline`, or `Checking the API…`). When the API is down, the page also shows the one
  banner with the backend start command.
- **Navigation**: nine routes, in this order, with these labels (`PAGES` and `PAGE_LABELS` in
  `src/hooks.ts`; the route ids are unchanged, so old deep links still work):

  | Route | Label | Route | Label |
  |---|---|---|---|
  | `#/overview` | Overview | `#/memory` | Experience Memory |
  | `#/live` | Incident Lab | `#/safety` | Safety & Approvals |
  | `#/timeline` | Trace Explorer | `#/evals` | Evaluations |
  | `#/evidence` | Evidence & Decisions | `#/architecture` | Architecture & Learning |
  | `#/knowledge` | Knowledge (new in M4) | | |

  A sidebar on wide screens; below 920 px a fixed brand bar with a **Menu** button
  (`aria-expanded`, `aria-controls`) that opens the list; it closes on navigation, Escape
  (focus returns to the button), a click outside or tabbing out. The browser tab reads
  `<page label> · RECAP Mission Control`.
- **Top bar**: on a run-scoped page (`RUN_SCOPED_PAGES`: Trace Explorer, Evidence & Decisions,
  Experience Memory, Safety & Approvals; and the Incident Lab's run), **Selected run** with that
  run's `DataSourceBadge` read from its record (`provider_info`, `environment`), e.g.
  `REPLAY · RECORDED REAL-LLM RUN qwen3:8b / SIMULATED TARGET`; elsewhere the server's
  **Default provider**. Then approvals waiting (a link to Safety & Approvals) and sign-in.
- **Keyboard and landmarks**: a "Skip to main content" link first (it moves focus to `<main>`
  without touching the hash route), `<nav aria-label="Primary">` with `aria-current="page"` on
  the current entry, a `<header>`, `<main id="main">`, the environment strip as a labelled
  region; visible focus rings everywhere. Usable down to a 390 px phone: no page-level
  horizontal scroll (wide tables scroll inside their own box), key/value lists stack.
- **Styling**: the existing dark token palette (`:root` in `src/styles.css`) plus a spacing
  scale (`--space-1`…`--space-8`, 4 px grid) and type scale (`--fs-xs`…`--fs-2xl`); no
  gradients or decoration. The shell's geometry is in `--env-h`, `--topbar-h`, `--bar-h` and
  `--sticky-top` (`html` scroll padding uses it, so anchors land below the bars).

## Shared components

In `src/components/`; each has component tests in `tests/`. Use them on every page.

| Component | Props (main) | Notes |
|---|---|---|
| `StatusToken` (`StatusToken.tsx`) | `tone`, `label`, `icon?`, `detail?`, `title?`, `size?: "sm"\|"md"`, `variant?: "soft"\|"outline"\|"plain"` | Icon + text, never color alone (each tone has its own icon shape, `TONE_ICONS`). `StateToken({state})` and `stateIcon(state)` for lifecycle states; `StateChip`, `OutcomeChip` and `HypothesisChip` (`Badge.tsx`) now show icons instead of dots. |
| `DataSourceBadge` | `provider` (ProviderInfo fields: `origin`, `kind`, `model`, `replay_of`, `red_team`), `environment?` (`is_simulated`, `kind`, `name`, `description`; omit = source only, `null` = "TARGET NOT RECORDED"), `size?`, `label?` | SCRIPTED BASELINE · LOCAL LLM · FREE HOSTED LLM · PAID LLM · REPLAY · RECORDED REAL-LLM RUN, then SIMULATED TARGET / LIVE TARGET. Pure `describeSource` / `describeTarget`; `summarySource(run)` maps a typed run summary (`provider_class`, `provider_kind`, `model`, `environment_*`, `replay`) onto the props. Never inferred from a name: no `origin` (and no scripted/replay `kind`) reads SOURCE NOT RECORDED. |
| `EnvironmentBanner` | `environment?` (from `/system`), `health?` (from `/health`), `children` (right slot) | Pure `environmentStatus(environment, health)`. |
| `LoadingView`, `EmptyView`, `ErrorView`, `PolledView` (`StateViews.tsx`) | `LoadingView({label?, inline?})`; `EmptyView({title, reason, icon?, action?})` (a reason is required); `ErrorView({error, title?, onRetry?, retryLabel?})`; `PolledView({polled, children: (data) => …, empty?: (data) => reason \| null, emptyTitle?, loadingLabel?, errorTitle?, idle?})` | `PolledView` renders a `usePolling` result: loading, error with Retry (= `refresh`), empty with its reason, the data; a failed refresh keeps the data with a notice. An unreachable API makes views wait (the shell shows one banner). The older `EmptyState`, `ErrorState` (now with `onRetry?`), `Loading` and `ErrorBoundary` (now with "Try again") render these. |
| `Modal` | `open`, `onClose`, `title`, `description?`, `children`, `footer?`, `size?: "small"\|"medium"\|"large"`, `initialFocusRef?`, `closeLabel?`, `dismissible?` | `role="dialog"`, `aria-modal`, labelled by its title, described by `description`; focus moves in, Tab/Shift+Tab wrap (`trapTarget`), Escape closes, focus returns to the opener, the page does not scroll behind it. Rendered at the end of `<body>`. Closing is never a decision: an approval shown in a modal stays pending. |
| `FilterBar` | `filters: {key, label, options, display?, allLabel?}[]`, `values: {key: value \| null}`, `onChange(key, value)`, `onClear?`, `summary?`, `label?` | Up to 6 options: toggle chips (`aria-pressed`, a check mark when pressed); more: a labelled select. Build options from the data with `filterOptions(items, read)` (counts; rounds sort numerically; an item may have several values) and filter with `applyFilters(items, values, readers)`. A deep-linked value missing from the data stays visible. |
| `Pager` | `offset`, `limit`, `total?` (or `shown` + `hasMore` when the server reports no total), `onChange(offset)`, `pageSizes?`, `onPageSizeChange?`, `noun?` | A labelled `<nav>`, "51–100 of 1,234 events", First/Previous/Next/Last. `pageWindow(...)` clamps a bad offset (use its `offset`); `paginate(items, offset, limit)` for lists held in memory. |
| `LogView` | `text`, `label`, `maxHeight?` (320), `initialLines?` (120), `maxChars?` (20 000), `untrusted?: boolean \| string`, `wrap?`, `downloadName?`, `emptyText?` | A bounded monospace box that scrolls (focusable, named); long text is cut at a line (never inside a character) with Show more / Show all; optional `.txt` download of the full text. |
| `UntrustedText`, `safeHref`, `SafeLink` (`Untrusted.tsx`) | `UntrustedText({text, author?, quote?})`; `safeHref(url)`; `SafeLink({href, children})` | Model or tool text as an escaped, marked quote. Only http(s) and in-app addresses become links (`javascript:`, `data:`, obfuscated variants are refused). |

**Untrusted text** (anything a model or a tool wrote: claims, summaries, queries, log lines,
runbook text, observations) is always rendered as React text: escaped, never HTML, never a link
target unless `safeHref` accepts it. A test fails if any source under `src/` injects raw HTML.


## Pages

Navigation is hash-based, so every view is a shareable deep link:
`#/timeline?run=<run_id>`, `#/evidence?run=<run_id>&ev=<evidence_id>`, `#/knowledge?q=<query>`.
The selected run follows you between the run-scoped pages (Trace Explorer, Evidence & Decisions,
Experience Memory, Safety & Approvals). Without `?run=` they show the latest run.

| Page | What it shows | API |
|---|---|---|
| **Overview** | The implementation behind each interface (environment adapter, provider, retriever, router, policy, oracle, transport, memory size); for runbook search, the default mode and whether hybrid can run (`hybrid_ready`; the retriever names what a run that does not choose uses); run counts by final state; the latest evaluation headline (labeled *synthetic benchmark · simulated · small N*); recent runs (click a run to open its timeline). | `/system`, `/runs`, `/evals/results` |
| **Incident Lab** (`#/live`) | Scenario picker (demo scenarios first; ground truth only behind *Show ground truth*). Config: strategy, RAG, runbook search (BM25 or hybrid), memory, approval mode (manual = you approve here, auto = simulation auto-approver), simulated tool latency. **Run incident** starts a run and streams it: a lifecycle stepper, a readable live event feed (click a line for the raw event; a claim, search query or summary an agent's provider wrote is quoted, RECAP's notes on a summary are kept apart), an approval card with the exact proposal, and a result panel (the diagnosis as RECAP's checked fields with the claim quoted, disagreements and the verifier's leads, oracle verdict, approval, memory partitions and episode, usage, budget, and the backend's own cost line from `run.summary`). | `POST /incidents/run`, `/runs/{id}/stream` (SSE), `/approvals/pending`, `POST /approvals/{proposal_id}`, `/runs/{id}` |
| **Trace Explorer** (`#/timeline`) | A Gantt-style chart: one lane per agent plus the controller. Bars are MCP tool calls (`tool.call` paired with `tool.result` by `invocation_id`). Shaded spans show when each agent was active; dots are controller decisions, denials and injection flags. A bracket marks the measured window where parallel specialists ran concurrently (`overlap_ms`). Long human approval waits can be compressed into an axis break. Includes a table view and, per round, the routing decision (mode, rule, reason, signals, step allocation), the verifier's open questions and leads, and per agent its outcome, claims, evidence, runbook searches, citations (red-team overlay chunks marked) and summary. Each claim's verdict comes from the last verification that read it: the final one, the one before a fix that did not recover the system, or none (a finding a budget stop kept is marked *partial*). | `/runs/{id}/events`, `/runs/{id}` |
| **Evidence & Decisions** (`#/evidence`) | The evidence registry (id, tool, source ref, agent, trust level, flags such as `prompt_injection_suspected`, token estimate, what the final verification relied on; expand a row for the raw content); hypothesis checks with passed/failed rules, proposers, basis and the release or dependency each claim was judged by; disagreements (each settling rule) and the verifier's leads for the next round, with the merge history; every runbook search with its ranking signals and the retriever it actually used; retrieved runbook chunks as citations (overlay sections marked); a knowledge search box. Runs recorded before M3 read *not recorded*, never *none*. | `/runs/{id}`, `/runs/{id}/events`, `/knowledge/search` |
| **Knowledge** (`#/knowledge`) | The runbook index (documents and their sections, the chunk ids citations use; red-team overlay documents marked); the retrieval configuration from `GET /system` (default search mode, the default retriever, whether hybrid search can run, the stored vectors' embedding model, the vector index identity and knowledge overlays from `features` when the server reports them, indexed sections, fixtures fingerprint; *Not recorded* when the server does not report a field); a search box (`?q=` and `?k=` in the route) whose results show rank, score, the retriever that ranked each one, a reranker when reported, ranking signals, injection flags and parsed action hints, with the section text as untrusted data in a bounded box. Run-level (agent-initiated) searches are on Evidence & Decisions for now. | `/system`, `/knowledge/docs`, `/knowledge/search` |
| **Experience Memory** | All episodes (outcome, oracle-verified, partition worded by who made the decisions, since a demo episode from before M3 may be a real model's; successful and unsuccessful actions, provenance; expand a row for evidence summaries and the run summary, each checked claim with its release or dependency). For a run: the partitions it read (a replay: *memory as of* its recording), then its memory hits with similarity, past outcome, mechanism match, **safe to reuse YES/NO** and the conditions behind it. *Similar ≠ safe to reuse.* | `/memory`, `/runs/{id}`, `/runs/{id}/events` |
| **Safety & Approvals** | Pending approvals (approve or reject them here, signed in); the append-only approval decision log; for a run: mitigation policy verdicts with rule ids (the last id is the deciding rule), every gateway decision in the trace, proposals (declared vs trusted risk), approvals (single use, consumed), mitigation attempts (denied ones in red) and prompt-injection flags with the flagged excerpt. Also a static legend of the policy rule ids (documentation, not metrics). | `/approvals/pending`, `/runs/{id}`, `/runs/{id}/events` |
| **Evaluations** | **Run evaluation** starts the protocol in the background and shows its progress; if it fails, the page shows the error. Results: the test-suite table per configuration, the red-team table, per-case outcomes (filterable), the caveats and reproducibility metadata. Labeled *Synthetic benchmark on the simulator · small N · no statistical claims · not SREGym*. | `POST /evals/run`, `/evals/results` |
| **Architecture & Learning** (`#/architecture`) | A layered HTML/CSS diagram of the RECAP flow with trust boundaries (dashed amber = untrusted data, solid teal = trusted code), next to the live wiring from `/system`. Swapping the infrastructure means writing one `TargetEnvironment` adapter and changing `configs/environment.yaml`: the controller, gateway, RAG, memory, router and orchestrator code stay unchanged. Still simulator-specific (data, not code): the log conventions the scripted policy and verifier parse, the policy allowlists and the runbooks. | `/system` |

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
  instead of failing (a view's own errors offer **Retry**).

## Code layout

```
src/
  main.tsx               entry point (React StrictMode)
  App.tsx                fetches what the shell shows (/health, /system, pending approvals, the selected run)
                         and picks the page for the route
  api.ts                 typed fetch helpers, ApiError (offline / HTTP status / FastAPI detail), SSE helper
  auth.ts                the operator token (sessionStorage, memory fallback) and the Authorization header
  types.ts               TypeScript mirror of recap/core/models.py, API responses and the eval report
  hooks.ts               routes (PAGES, PAGE_LABELS, RUN_SCOPED_PAGES, parseHash, hrefFor, navigate),
                         usePolling, useRunEvents (SSE + polling fallback), useRunSelection
  format.ts              state/verdict colors, number and time formatting, honest labels (environment
                         badge, provider, cost), describeEvent (event → one line), splitSummary
  view/                  pure M3 view helpers (findings, verification, retrieval, memory), tested in tests/
  styles.css             the whole theme (tokens, shell, shared components; no framework); styles/*-m3.css per page
  components/            Shell (AppShell, NAV, the top bar's data source), EnvironmentBanner, DataSourceBadge,
                         StatusToken, StateViews, Modal, FilterBar, Pager, LogView, Untrusted; and Badge/Chip,
                         Card (+ StatTile, Meter, Notice, Segmented, Toggle), Table, StateStepper, EventFeed,
                         ApprovalCard, OperatorSignIn, Timeline (Gantt), JsonView, RunPicker (run scope bar),
                         EmptyState (older state views, error boundary), Icon
  pages/                 one file per page (Knowledge.tsx is new in M4)
scripts/test.mjs         npm test (see Testing)
tests/                   *.test.ts: pure helpers · *.test.tsx: components · support/: the console guard and
                         render helpers for component tests
```

Event payloads are read defensively: every payload key is optional in `types.ts`, so a missing
or renamed key degrades one line of text instead of breaking a page. The categories and keys
used are the ones the controller, harness and gateway emit (see `recap/telemetry/tracer.py`
`TraceCategory` and the `emit(...)` calls in `recap/harness/`).

## Testing

`npm test` runs `scripts/test.mjs` (no test packages, nothing to install):

1. `tests/*.test.ts` (the pure helpers in `src/format.ts`, `src/auth.ts`, `src/view/`): Node runs
   them as they are (type stripping, Node 22.18+).
2. `tests/*.test.tsx` (React components): Node cannot run TSX, so each file is bundled first with
   the esbuild that ships with vite (`node_modules/esbuild`) into
   `node_modules/.cache/recap-dashboard-tests/` (ESM, React's development build, inline source
   maps, CSS imports ignored, `import.meta.env` defined as Vite would).
   `tests/support/setup.ts` is injected into every bundle: **any `console.error` or
   `console.warn` during a test fails it**, and React's warnings (a list without keys, an
   unknown DOM attribute, invalid nesting...) arrive that way.
3. Both run in one `node --test` call: one report, one exit code. CI runs the same `npm test`.

`node scripts/test.mjs tests/modal.test.tsx` runs only the files named.

Component tests render with `react-dom/server` (`render()` in `tests/support/render.tsx`, which
also fails on console output) and assert on the markup with small helpers (`textOf`, `tags`,
`withAttr`, `count`, `polled`): labels, roles, `aria-*` attributes, that hostile text (`<script>`,
`onerror=`, `javascript:` links) comes out escaped, and the loading / empty / error states.
Effects do not run in a server render, so behavior that needs a browser is kept in pure
functions and tested there (`trapTarget` for the modal's focus trap, `pageWindow`, `sliceLog`,
`filterOptions`, `describeSource`, `environmentStatus`, ...).

Checks before a commit, from `apps/dashboard`: `./node_modules/.bin/tsc --noEmit`, `npm test`,
`npm run build`.
