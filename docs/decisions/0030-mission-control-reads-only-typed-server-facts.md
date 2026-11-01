# ADR 0030: Mission Control shows only typed server facts, one screen per question, and makes the replay watchable without changing it

**In one sentence:** the dashboard reads the typed, read-only API of
[ADR 0027](0027-a-typed-read-only-api-over-the-run-records.md) through types generated from the
API's own response models, shows each fact with where it came from (scripted policy, replay of
a recorded real-model run, live model; always a simulated target), renders every text a model
or tool wrote as escaped, labelled data, never closes the approval dialog for the operator, and
lets a replay run slowly enough to watch, without changing what it decides.

## Status

Accepted (M4 step 3, 2026-10-26). Builds on [ADR 0015](0015-operator-auth-and-no-wins-approvals.md)
(the operator token and approvals), [ADR 0027](0027-a-typed-read-only-api-over-the-run-records.md)
(the typed views), [ADR 0028](0028-content-addressed-recordings-and-replay-by-manifest-id.md)
(replay by manifest id) and [ADR 0029](0029-one-evaluation-record-per-run-and-modes-that-never-share-a-number.md)
(evaluation records).

## Context

The dashboard is where most people meet RECAP, often in a live demo. Up to M3 it was a set of
pages that worked out run facts in the browser from raw records and trace events. M4 asks for a
product-grade control center with nine views, and for one thing above all: that nothing on
screen can be mistaken for more than it is. Four risks drive this decision:

- **A screen that infers.** A number computed in the browser from raw events is a claim no
  server check stands behind, and each page computes it a little differently.
- **A screen that blends.** A run decided by the scripted policy, a replay of a recorded model
  run and a live model run look alike once drawn; a demo could present a replay as live
  reasoning, or a scripted success rate as a model's.
- **A screen that executes text.** Model answers, log lines and runbook text are written by
  parties RECAP does not trust; shown as HTML or Markdown they could run script in the
  operator's browser, the one place that holds the operator token.
- **A demo that is too fast to see, or that cuts a corner.** A replay answers from its
  recording at once, so the recorded 11-minute run finishes in about two seconds; and a
  dismissible approval dialog invites a click-through.

## Decision

1. **Typed server facts only.** Every screen reads the typed API (`src/api.ts`); its types in
   `src/dto.ts` are generated from `apps/api/dto.py` by `scripts/gen_dashboard_types.py`, and a
   Python test fails when they drift. A fact the server lists in `not_recorded` is shown as
   "Not recorded", never computed or defaulted.
2. **One screen per question**, nine routes: Overview (is the system ready, what ran),
   Incident Lab (run or replay one incident, approve its fix), Trace Explorer (who did what,
   when, in which round), Evidence & Decisions (what the verifier accepted and why), Knowledge
   (what was looked up), Experience Memory (what was remembered and whether it was safe to
   reuse), Safety & Approvals (what was allowed, asked or denied, and by whom), Evaluations
   (batches by mode) and Architecture & Learning (the implementation and its status).
3. **Provenance on screen.** The environment strip comes from `GET /system` only (SIMULATED
   ENVIRONMENT, or ENVIRONMENT NOT CONFIRMED before the server answers); each run carries its
   data source (`DataSourceBadge`: SCRIPTED, LOCAL LLM, FREE HOSTED LLM, or REPLAY · RECORDED
   REAL-LLM RUN, which rests on the manifest match of ADR 0028, not on a file name);
   evaluation numbers are counts per batch of one mode ("k of N"), never a blended rate.
   Planned parts (Kind, SREGym) are labelled planned, never active.
4. **Untrusted text is data.** Every text goes through React's escaping; text a model or tool
   wrote is quoted and labelled with its author (`UntrustedText`, `LogView` with bounded
   length and a plain-text download); links only for http(s) and in-app addresses
   (`SafeLink`); a test fails if any dashboard source renders raw HTML.
5. **The approval cannot be dismissed.** The approval dialog (`Modal` with
   `dismissible={false}`) shows the exact action, target, parameters, trusted risk tier, a short
   digest and the expiry, and closes only on an explicit Approve or Reject; manual mode never
   approves by itself. The operator token stays in the tab's session storage and is never
   rendered.
6. **A watchable replay that cannot change the outcome.** The Incident Lab starts a replay of a
   committed recording by manifest id with `replay_pace` 0.05 by default ("Watchable", about
   half a minute for the golden run) or 0 ("Instant"). The pace only delays each recorded
   answer (at most 15 s); requests are still matched by content and stops by place
   (ADR 0026), so the run decides exactly what the recording decided, and its fix still waits
   for a fresh human approval.
7. **No new dependencies for quality checks.** Component tests bundle with the esbuild that
   ships with Vite and run on Node's own test runner (a React warning or console error fails a
   test); the browser smoke test drives the installed Chrome over the DevTools protocol with
   Node's built-in WebSocket.

## Consequences

### Good

- What a viewer sees is what the server recorded, in the server's words; a renamed field breaks
  the build, not the demo.
- A replay, a scripted run and a live model run cannot be confused on any screen, and neither
  can a simulated target and a real one.
- Hostile text in logs or model answers is shown, quoted, and harmless.
- The golden demo is watchable in real time and still exercises the real approval path.

### Bad

- More server work: every new screen fact needs a field in a response model first (and a
  regenerated `dto.ts`).
- Generated types say what the server sends, not what a screen should show: a page still has
  to decide how to present "not recorded".
- A paced replay takes about half a minute instead of two seconds; the "Instant" choice remains
  for a quick check.

## Alternatives considered

- **Compute run facts in the browser from raw events.** Simpler server, but every screen would
  re-derive and could disagree; rejected (ADR 0027).
- **Hand-written TypeScript types for the new endpoints.** Rejected: they drift silently; the
  generator plus a drift test costs one script.
- **Render model answers as Markdown.** Nicer formatting, but a Markdown renderer that allows
  HTML is a script-injection path in the operator's browser; plain, escaped text instead.
- **Playwright for browser tests.** It downloads browsers (a heavy install that needs the
  owner's approval); the installed Chrome over the DevTools protocol covers the smoke test.
- **Show a replay at its original speed.** Faithful but about 11 minutes long; the pace is a
  share of each original duration, capped, so the order and the relative timing stay visible.
