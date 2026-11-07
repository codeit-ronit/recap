# `apps/dashboard` — RECAP Mission Control

**RECAP Mission Control** is a dark, restrained React + TypeScript (Vite) single-page app for
demonstrating RECAP: the incident lab, the multi-agent trace, evidence and decisions, the runbook
knowledge base, experience memory, safety and approvals, evaluations and the architecture.

![RECAP Mission Control, the Overview page](../../docs/screenshots/overview.png)

**Everything on screen comes from the RECAP API** (`apps/api/main.py`). The dashboard has no
hardcoded metrics, sample data or fake runs. If there are no runs yet, the pages say so. By
default RECAP runs against a **simulated** environment (a simulated shop). Since M5 a server
started with a target profile (`RECAP_TARGET=kind-demo` or `sregym-hotel`) runs against a real
Kubernetes cluster instead: a Kind cluster the owner runs, not production. Nothing on screen
suggests a production system. A strip across the top of every page says which environment is
plugged in, from the server only: **SIMULATED ENVIRONMENT** (amber) when `GET /system` reports
`environment.is_simulated`, a red **LIVE ENVIRONMENT · &lt;kind&gt;** when it reports a target
that is not simulated (`kind` for the Kind demo, `sregym` for SREGym's cluster), the label
`GET /health` sends until `/system` answers, and **ENVIRONMENT NOT CONFIRMED** before either
does (never a guess).

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

### With demo data (what the screenshots show)

```bash
python scripts/seed_demo.py --data-dir ~/recap-demo     # real offline runs: scripted, and replays of the committed recordings
RECAP_DATA_DIR=~/recap-demo uvicorn apps.api.main:app   # http://127.0.0.1:8000/
```

`scripts/seed_demo.py` refuses the repository's own `data/` folder, so demo runs never mix with
real experience. Each seeded run is either the scripted policy (labelled SCRIPTED) or a replay
of a committed real-model recording (labelled REPLAY · RECORDED REAL-LLM RUN); none calls a model.

### On a real cluster (M5, the owner's setup)

```bash
cd apps/dashboard && npm install && npm run build && cd ../..
bash infra/aws/ec2.sh hold env RECAP_TARGET=kind-demo RECAP_DATA_DIR=$HOME/recap-kind-runs \
  .venv/bin/uvicorn apps.api.main:app            # http://127.0.0.1:8000/
```

This needs the owner's VM, the cluster, RECAP's identities and the SSH tunnel first
([docs/m5-report.md](../../docs/m5-report.md#11-reproduction), section 11). The strip then
reads LIVE ENVIRONMENT · kind. On Overview, **Check now** runs the cluster adapter's own
preflight (`POST /environment/check`: operator only, nothing changes, no run starts); the
Incident Lab offers manual approval only (the server refuses the auto-approver on a live
target); and the approval dialog asks for the target service's exact name before Approve.
M5's acceptance run, `run-34a3580d2f`, was started and approved this way
([its export](../../evals/live/2026-11-07-qwen3-8b-kind-dashboard-approved-prompts-v4.7/README.md)).

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
  | `#/knowledge` | Knowledge | | |

  A sidebar on wide screens; below 920 px a fixed brand bar with a **Menu** button
  (`aria-expanded`, `aria-controls`) that opens the list; it closes on navigation, Escape
  (focus returns to the button), a click outside or tabbing out. The browser tab reads
  `<page label> · RECAP Mission Control`.
- **Top bar**: on a run-scoped page (`RUN_SCOPED_PAGES`: Trace Explorer, Evidence & Decisions,
  Knowledge, Experience Memory, Safety & Approvals; and the Incident Lab's run), **Selected run** with that
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
Knowledge, Experience Memory, Safety & Approvals). Without `?run=` they show the latest run. Their run
bar names each investigation round's routing (`R1 parallel`, `R2 parallel`).

| Page | What it shows | API |
|---|---|---|
| **Overview** | *Start the demo* (the golden recording, one click to its replay panel), or on a live server a real-cluster call to action instead (an approved action changes the cluster; approving takes the target service's typed name); system readiness (environment adapters: the simulated shop, Kind and SREGym implemented since M5, only the configured one operational; components and their status); the 10 newest runs with their data source; outcomes counted separately per data source and target (scripted, replay, local LLM, free hosted LLM; the simulator apart from a real cluster: never one success rate); what is running now; the *Active environment* (adapter, namespace, the actions it supports, its scenarios, the policy and the oracle; on a live target the time a fix may take to settle) with **Check now**, which asks the server to reach the target as a run would (`POST /environment/check`: operator only, nothing changes, no run starts); access (reading is open, acting needs the operator token); anything the server still lists as planned (a server of this repository lists none since M5); the decision providers (class, readiness, per-run selection; an API key only as set or missing). | `/system`, `/providers`, `/replays`, `/runs`, `/scenarios`, `/auth/session`, `POST /environment/check` |
| **Incident Lab** (`#/live`) | *Replay a recorded incident*: the committed real-model recordings (integrity-checked against the manifest) at a watchable or instant pace; the replay asks a person to approve again. *Run a scenario*: a scenario, a backend this server lets a run pick, the settings (on a live target, approval is manual only), and what keeps every run safe. A started run streams: the data-source banner, the stage stepper, the live event feed (key or all events; click a line for the raw event), *So far* counters, the **approval modal** (action, target, params, trusted vs declared risk, digest, expiry, the policy and evidence-gate checks, the model's rationale and cited evidence as untrusted text; Approve, Reject, Decide later, and *Test binding* on the simulated shop only; on a live target a red LIVE block naming the cluster and namespace, and a field where the target service's exact name must be typed before Approve), *Stop run*; then the outcome (the oracle's verdict, the approval, wall time) and the run details. | `/replays`, `/providers`, `/scenarios`, `POST /incidents/run`, `/runs/{id}`, `/runs/{id}/stream` (SSE), `/runs/{id}/events`, `/approvals/pending`, `POST /approvals/{proposal_id}`, `/runs/{id}/evidence`, `POST /runs/{id}/cancel`, `/memory` |
| **Trace Explorer** (`#/timeline`) | *This run* (data source, model, prompt version, timing, counts, replay provenance, the server's notes on reading its times); the *run timeline* (one lane per agent plus the controller: tool calls, model answers, controller decisions, rounds and the measured window where agents ran concurrently; chart or table); *step by step* (before the first round, each round's routing, each agent's steps side by side with its context pack, model requests and tool calls, its claims and the verifier's verdict, then the fix); the *event log* (filter by agent, round, tool or kind; paged). On a real-cluster run (M5) it also says where the alert was measured, what the write changed on the cluster as RECAP read it back, and how long the oracle probed. | `/runs/{id}/timeline`, `/runs/{id}` |
| **Evidence & Decisions** (`#/evidence`) | Counts; *the decision* (the supported diagnosis apart from the claims not accepted, with the rule lines that decided); the causal chain per round and agent; disagreements; *from diagnosis to action* (the proposal, the evidence gate, the safety policy); the *evidence registry* (tool, agent, trust class, injection flags, how the verifier used each item; expand a row for the original observation in a bounded box). | `/runs/{id}/evidence` |
| **Knowledge** (`#/knowledge`) | *This run's retrieval*: search mode and embedding model, every runbook search (who wrote the query, ranks, scores, signals, what used each section), agent-initiated and refused searches, the sections in context, and its memory search. *The knowledge base*: the retrieval configuration, a search box (`?q=`, `?k=`) and the runbook index. A field a run did not record reads *Not recorded*. | `/runs/{id}/retrieval`, `/system`, `/knowledge/docs`, `/knowledge/search` |
| **Experience Memory** | How partitions keep kinds of runs apart (who reads and writes what); the episodes grouped by partition (that run's result, oracle verification, who made the decisions, the source run; expand for evidence and reuse conditions; red-team plants marked); for one run: what it read and wrote, its memory hits and RECAP's reuse verdict on each (*similar is not safe to reuse*). | `/memory`, `/system`, `/runs/{id}/retrieval`, `/runs/{id}` |
| **Safety & Approvals** | Approvals waiting (decide here, signed in; on a live target Approve takes the target service's typed name, as in the Incident Lab); the four layers (policy, evidence gate, exact approval, signed capability) with their status; for one run: unsafe actions *attempted* apart from *executed*, each layer's verdict, the permission lifecycle from the approval log, what RECAP stopped, the policy decisions (filterable), prompt-injection flags and the run's approval log; across runs: the approval audit log, runs that ended in a safety stop, what the adversarial tests check, and the rule reference. | `/approvals/pending`, `/approvals/events`, `/approvals/{id}/events`, `/runs/{id}`, `/runs/{id}/evidence`, `/runs/{id}/timeline`, `/runs?state=BLOCKED`, `/system` |
| **Evaluations** | One tab per mode, never combined: scripted regression, offline replay, real LLM (exploratory) and real Kubernetes (since M5: the Kind demo batch and the SREGym batch). Per batch: plan, providers, models, prompt versions, N and what the runs worked on (the simulated shop, or a named cluster marked live, for the batch and for each record); outcomes as *k of N* with a small-sample warning; on SREGym's batch, SREGym's grader verdicts in a tile of their own, never merged with RECAP's outcome; the configuration × scenario matrix; why runs did not recover (one failure category each); every run (filterable, paged; tokens reported vs estimated); methods and limitations; how to make a batch (the commands, run from a terminal). The scripted regression can be started here (operator only). | `/evals`, `/evals/{id}`, `/runs`, `/system`, `POST /evals/run`, `/evals/results` |
| **Architecture & Learning** (`#/architecture`) | The flow of one incident (alert → controller → retrieval & memory → router → agents → MCP gateway → verifier → approval → authorized write → oracle → episode), each step with its status from `/system`, what it does, its code, its ADRs and the trace events it writes (a part the server lists as planned is marked so, never active); since M5 also *How a real Kubernetes action is authorized*, the chain an approved fix passes through on the Kind demo and on SREGym's cluster; the live wiring and versions; links to the docs. | `/system` |

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
- **A replay** (Incident Lab, *Replay a recorded incident*) is `POST /incidents/run` with
  `{scenario_id: <the recording's scenario>, config: {provider: "replay", replay_fixture: <its
  manifest id>, approval_mode: "manual", replay_pace}}`; the server takes routing, memory and
  retrieval from the recording's manifest entry. `replay_pace` 0.05 (*Watchable*, the default)
  waits 5% of each recorded answer's original time, so the run reaches the approval in about
  half a minute; 0 (*Instant*) in a few seconds. Only the timing changes, never what is replayed.
- While a run waits at `AWAITING_APPROVAL`, the page polls `/approvals/pending`. Approve and
  Reject send the proposal's exact digest; the server records the signed-in operator as the
  approver. The server refuses any other digest, an expired request or a second decision with
  **409**, and the card shows that response. A rejection also revokes an approval that was not
  used yet. Any decision refreshes the top bar's *approvals waiting* count at once (the window
  event `recap:approvals-changed`). *Test binding* deliberately sends a tampered digest to show the refusal; the
  request stays pending. It is offered on the simulated shop only.
- **On a live target** (M5) Approve also needs the target service's exact name, typed in the
  dialog: it is sent with the approval as `confirm_target`, and without it, or with another
  name, the server answers **400** and decides nothing. Reject needs no name. The decision log
  records that the name was typed and how long the answer took; it proves a holder of the
  operator token and a timing, not which person decided.
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
  view/                  pure view helpers per page (findings, verification, retrieval, memory, lab, timeline,
                         safety, evals, system), tested in tests/
  styles.css             the theme (tokens, shell, shared components; no framework); styles/*.css per page
  components/            Shell (AppShell, NAV, the top bar's data source), EnvironmentBanner, DataSourceBadge,
                         StatusToken, StateViews, Modal, FilterBar, Pager, LogView, Untrusted; and Badge/Chip,
                         Card (+ StatTile, Meter, Notice, Segmented, Toggle), Table, StateStepper, EventFeed,
                         ApprovalCard, OperatorSignIn, Timeline (Gantt), JsonView, RunPicker (run scope bar),
                         EmptyState (older state views, error boundary), Icon
  pages/                 one file per page (Knowledge.tsx is new in M4)
scripts/test.mjs         npm test (see Testing)
e2e/smoke.mjs            the browser smoke test against a running server (see Testing)
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

### Browser smoke test (`e2e/smoke.mjs`)

`npm test` cannot see layout, the console or the live stream, so `e2e/smoke.mjs` drives a real,
installed Google Chrome (headless, a throwaway profile) through the Chrome DevTools Protocol, with
Node's own `fetch` and `WebSocket` (Node 22+; nothing to install), against a **running** server
that serves the built dashboard:

```bash
python scripts/seed_demo.py --data-dir /tmp/recap-demo            # a separate demo folder
cd apps/dashboard && npm run build && cd ../..
RECAP_DATA_DIR=/tmp/recap-demo RECAP_OPERATOR_TOKEN=<a throwaway token, 16+ characters> \
  uvicorn apps.api.main:app --port 8000
cd apps/dashboard
node e2e/smoke.mjs --base http://127.0.0.1:8000 --token <the same token>
```

1. **Pages**: the nine routes at 1440 × 900 and at 390 × 844, the run-scoped ones for a replay
   of the golden recording, a scripted run, a BLOCKED run and a BUDGET_EXHAUSTED run (those the
   folder has). Each must show its heading and tab title, no error panel and nothing still
   loading; log nothing to the console (no error, warning or exception); never scroll sideways,
   also with every `<details>` opened; show the SIMULATED ENVIRONMENT strip and the selected
   run's data source (SCRIPTED BASELINE, REPLAY, ...); and never put the operator token or
   injected markup (`<script>`, `on*=` attributes) in the DOM.
2. **The live demo** (needs the token): sign in through the top bar, open the Incident Lab,
   replay the golden recording at the watchable pace, watch the stepper and the event feed
   advance, approve the exact action in the modal, see RESOLVED with the oracle's verdict, and
   check that the API agrees (RESOLVED, oracle verified, a replay, approved by
   `operator:<name>`). Then that run's Trace Explorer, Evidence, Knowledge, Memory and Safety
   pages, and the operator's name in the approval log.

It prints one PASS/FAIL line per check and exits 0 (all passed), 1 (a check failed) or 2 (it
could not run: no API, no Chrome). The demo starts one replay run on that server (no model is
called): point it at a demo folder, not at data you care about. Options: `--chrome <path>`
(default: `CHROME_PATH`, then the usual install paths), `--token-file <path>` or
`RECAP_OPERATOR_TOKEN` instead of `--token` (the token is never printed), `--pace instant`,
`--skip-flow`, `--skip-pages`, and `--screenshots <dir>`, which also saves the screenshots
below. `node e2e/smoke.mjs --help` lists them.

## Screenshots

The real dashboard on the seeded demo folder, taken by `node e2e/smoke.mjs --screenshots
../../docs/screenshots` (1440 × 900 unless noted). The run-scoped ones show the smoke test's own
replay of the golden recording, approved in the browser; `evidence.png` and `safety.png` show
the seeded `malicious-log` run (BLOCKED). **They are from 0.5.0, before M5:** they show the
simulated shop only, with Kind and SREGym still listed as planned and the Kubernetes
evaluations as a planned tab, and no live target, readiness check or typed-name approval.

| File | What it shows |
|---|---|
| [overview.png](../../docs/screenshots/overview.png) | Overview: the demo launcher, system readiness (simulated shop active, SREGym and Kind planned) |
| [incident-lab-approval.png](../../docs/screenshots/incident-lab-approval.png) | Incident Lab during the paced replay: the approval modal for `restart_service` on `inventory` |
| [incident-lab-resolved.png](../../docs/screenshots/incident-lab-resolved.png) | The same run after approval: every stage done, RESOLVED, the oracle's verdict, approved by the operator |
| [trace-explorer.png](../../docs/screenshots/trace-explorer.png) | Trace Explorer: two specialists in parallel for two rounds, tool calls and replayed model answers per lane |
| [evidence-decision.png](../../docs/screenshots/evidence-decision.png) | Evidence & Decisions: resource pressure on inventory supported; the cart-deployment claim not accepted, with the rules that decided |
| [evidence.png](../../docs/screenshots/evidence.png) | The evidence registry of the `malicious-log` run: one log entry flagged `prompt_injection_suspected`, all tool output untrusted |
| [knowledge.png](../../docs/screenshots/knowledge.png) | Knowledge: how the run searched (BM25) and its memory search |
| [memory.png](../../docs/screenshots/memory.png) | Experience Memory: one episode per demo run with its result, oracle verification and the action it took |
| [safety.png](../../docs/screenshots/safety.png) | Safety & Approvals for `malicious-log`: 1 unsafe action attempted, 0 executed; `delete_namespace` denied by `R9-destructive-deny` |
| [evals.png](../../docs/screenshots/evals.png) | Evaluations: one tab per mode, never combined; the scripted regression batch |
| [architecture.png](../../docs/screenshots/architecture.png) | Architecture & Learning: the flow of one incident with each step's status from `GET /system` |
| [phone-overview.png](../../docs/screenshots/phone-overview.png), [phone-incident-lab.png](../../docs/screenshots/phone-incident-lab.png) | The same dashboard at 390 px: Overview, and the resolved replay in the Incident Lab |
