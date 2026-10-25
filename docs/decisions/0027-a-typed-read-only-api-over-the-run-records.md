# ADR 0027: A typed, read-only API over the persisted run records, and a safe per-run provider choice

**In one sentence:** screens and reviewers read every run through typed, versioned views that
the API builds only from what the run persisted (its checkpoint, its stored trace, its
evidence and the oracle's verdict), naming every gap instead of filling it, and a client may
choose the provider of one run only among the choices the owner allowed: the server's
default, the scripted baseline, a committed recording to replay, or a model provider marked
`api_selectable`.

## Status

Accepted (M4 step 1, 2026-10-26). Extends
[ADR 0015](0015-operator-auth-and-no-wins-approvals.md) (the operator-authenticated API: the
new choices go through the same doors) and
[ADR 0018](0018-record-every-model-exchange-and-replay-in-ci.md) (a committed recording can
now be replayed from the API, by its manifest id).

## Context

Until M3 the read endpoints returned what the stores hold: the raw `RunRecord`, the raw
trace events, and dictionaries without a declared shape. The dashboard then worked out
everything else itself: which agent ran in which round, how a run ended, how many tokens a
model reported, whether a replay was of a real model run. That has four problems:

- **Inference in the browser.** A screen that derives a claim the server never checked can
  show something no record supports, and every screen derives it a little differently. M4
  asks for the opposite: component status and run facts backed by server facts only.
- **No contract.** Nothing said which fields a response has, so a renamed field would have
  broken the dashboard silently, and `/openapi.json` described no response's fields.
- **Size and leaks.** A two-agent model run stores over a hundred trace events (the M3
  golden run: 128; longer runs are expected), which the dashboard fetched whole. And a model
  run's record names its recording by an absolute path when the data folder is outside the
  repository; that path went out in `GET /runs/{id}` (as did the absolute report folder in
  the answer of `POST /evals/run`).
- **A fixed provider.** The provider was server configuration only, so the committed real
  model runs could be replayed only by restarting the server with an environment variable:
  the dashboard could not offer "replay this recorded incident", nor the scripted baseline
  next to a model.

## Decision

1. **Typed contracts, one schema name** ([apps/api/dto.py](../../apps/api/dto.py)). Every new
   or extended read endpoint (system, providers, runs, the four run views, replays,
   evaluation batches) and the answer of `POST /incidents/run` declare a Pydantic
   `response_model`, so FastAPI validates each answer and `GET /openapi.json` documents
   every field. The schema is `recap-api-v1`: every response carries the header
   `X-RECAP-API-Schema`, and every typed JSON object also `schema_version`.
   Adding a field keeps the name; renaming or removing one needs a new name. `GET /runs` and
   `GET /providers` stay JSON arrays (the dashboard and a catalog test read them so), each item
   now typed, with the number of matching runs in `X-Total-Count`. `GET /runs/{id}` keeps
   `status`, `record`, `evidence` and `error` and adds a typed `summary`.
2. **Views are pure functions over the records** ([apps/api/views.py](../../apps/api/views.py)):
   they copy, count and match recorded values, and read nothing else; `RunReader`
   ([apps/api/service.py](../../apps/api/service.py)) does the reading. What each view is built
   from:

   | Endpoint | Built from |
   |---|---|
   | `GET /runs`, `GET /runs/{id}` | the checkpoint (state, history, provider info, versions, recovery), the server's own task status, the recordings manifest (which fixture a replay was), the exports in `evals/live/*/run.json` |
   | `GET /runs/{id}/timeline` | the stored trace events, page by page; lanes from the agents' findings (or the `agent.start` of a turn that has none), rounds from `routing.decision`, `investigation.round`, `orchestration.summary` and `verification.evidence` |
   | `GET /runs/{id}/evidence` | the evidence registry, the findings' hypotheses, each `verification.evidence` event, the final verification report, proposals, policy decisions, `safety.injection_flag` events |
   | `GET /runs/{id}/retrieval` | `retrieval.docs`, `retrieval.skip`, `memory.*` events, `RunRecord.retrieval`, `retrieved_docs`, `memory_hits` |
   | `GET /runs/{id}/usage` | `llm.call` events, `RunRecord.usage`, the `run.summary` event (budget used, the cost line), the provider's configured limits |
   | `GET /system` | `Runtime.system_summary`: the objects this runtime really built, and its settings |
   | `GET /providers` | the provider catalog (recap/providers/catalog.py) and configs/providers.yaml |
   | `GET /replays` | `fixtures/recordings/MANIFEST.json`, each file's SHA-256 against its pin |
   | `GET /evals`, `GET /evals/{id}` | `evals.records.find_batches` over `evals/reports/batches/` and `<data>/evals/batches/` |

3. **Gaps are named, never filled.** A field the record lacks is `null` and listed in
   `not_recorded` (a run older than a field, a missing trace, a request whose server reported
   no time). Tokens a server did not report stay `null`; RECAP's estimates are labeled
   `estimate: true`; no dollar figure appears except the controller's own cost line. A
   replay's requests carry the original run's durations, and the views say so. Request time
   minus the server's own time is called `outside_server_ms`, not a queue wait: Ollama's
   `total_duration` counts from the moment it received a request, so a wait in its queue is
   inside it. The committed golden recording shows this: 734.3 s as RECAP measured its 14
   chat requests, 733.4 s by Ollama's total, 474.3 s of Ollama compute.
4. **Who wrote what is part of the contract.** Claims, summaries and rationales carry their
   `author` (a model, a replayed model, the scripted policy); evidence its `trust_class`
   (untrusted tool output, runbook text, memory text, alert text, or RECAP's own); a timeline
   entry `untrusted_text`. Labels come from recorded facts: `SIMULATED ENVIRONMENT` only for a
   simulated environment, `RECORDED REAL-LLM RUN` only for a replay whose bytes are a
   committed fixture (the factory names it by the bytes' SHA-256).
5. **Large traces are paged, streams resume.** `GET /runs` filters by provider class,
   provider, scenario, state, environment and label, pages with `offset` and `limit`, and
   counts the matches. The timeline pages with a cursor (`after_seq`, `limit`,
   `next_after_seq`) after filters by agent, round, kind or tool. The event stream's `id` is
   the event's sequence number, and `Last-Event-ID` (or `after_seq`) resumes right after it.
6. **Nothing secret leaves.** Every response built from stored data (runs, traces, evidence,
   approvals, memory, knowledge, scenarios, evaluations), the event stream included, passes
   `LocalPaths.scrub`: the data folder becomes `data`, the checkout folder disappears, the
   home folder becomes `~`. Keys are reported only as present or not, as before; tokens and
   signed capabilities are never stored in a run's data to begin with (ADR 0014, 0015). A
   test reads each of these endpoints after a model run whose record names its recording by
   an absolute path, and finds no local folder, key, token or capability in any answer.
7. **`GET /system` reports real status.** Each component says what implements it and whether
   it is `active`, `available`, `unavailable`, `disabled`, `planned` or `error`. The SREGym
   adapter (an interface whose every operation raises) and a Kind cluster (no adapter) are
   `planned` for M5 and never `operational`, even when configured.
8. **A safe per-run provider choice** (`check_run_request`, `selection_rule`). A client may
   name, for one run: the server's default; the scripted baseline; `replay` with a
   `replay_fixture` that the manifest lists, whose bytes match their pin, recorded with the
   prompts this server renders, for the scenario the client names, with routing, memory and
   retrieval taken from the manifest entry (a conflicting setting is refused); or a model
   provider the owner marked `api_selectable: true` in configs/providers.yaml (a new
   `ProviderSettings` field, off by default; a paid provider marked so is refused when the
   settings load, and the API refuses a paid provider whatever its flag). Anything else is a
   400 that says why. The client names a recording only by its id, never by a path;
   `Runtime.make_provider` passes the id to the factory, which checks the bytes again before
   reading them. The local `ollama` provider is marked selectable ($0); `groq` is not (a click
   would spend a free quota).

## Consequences

### Good

- The dashboard can render runs, lanes, verdicts, retrieval and usage without working out
  facts itself, and a screen cannot show a field the contract does not have.
- `tests/e2e/test_api_contracts.py` drives real runs through the API for every outcome (a run
  still running, resolved, cancelled, rejected by the operator, out of budget, blocked by the
  policy, failed on a model server error, the golden real-model recording replayed with the
  operator's approval, an unknown run, a record without a trace) and validates every answer
  against its model; `tests/unit/test_api_views.py` covers old, partial and malformed records.
- The golden two-specialist run can be replayed from the API offline, with no model and no
  key, and its summary shows which recording it replayed and where the original run was
  exported.
- A reconnecting stream never repeats an event; a page of a long trace stays small.

### Bad

- The two list endpoints keep JSON arrays for compatibility, so their schema version is in
  the header only, and the total of `GET /runs` is a header.
- `GET /runs` filters in Python over the newest 10,000 runs (the run store has no query by
  provider), and each timeline, evidence, retrieval or usage request reads the run's whole
  trace before it builds one page. Measured on the 16 GB laptop with a synthetic run of
  10,000 small events: reading them took about 31 ms and building a 500-entry page about
  9 ms (the four exported real-model runs have 58 to 128 events). A shared server with many
  large runs would need a query in the store instead.
- A view can only show what was recorded. A BM25 search's duration, the vector file a hybrid
  run searched and a trace verdict's own citations were never recorded, so they show as not
  recorded; recording them is a change to the controller, not to the API. Likewise the time a
  model request waited in Ollama's queue: the trace keeps Ollama's total, not its compute
  time, so the wait cannot be shown until a provider records both.
- An outcome of `cancelled` is read from the controller's note ("run cancelled"), the only
  place a record keeps it; a test pins the wording.
- With `api_selectable` on for `ollama`, an operator can start local inference from the
  dashboard: $0, but it occupies this machine's memory and CPU for minutes.

## Alternatives considered

- **Versioned paths (`/v1/runs`, ...) next to the old ones.** Two copies of every endpoint to
  keep consistent, against the spec's "prefer incremental changes".
- **Envelopes for every list (`{items, total}`).** One rule for all responses, but it breaks
  the dashboard's run list and provider panel and a catalog test until they change together.
- **Let the dashboard compute the views.** That is the inference this decision removes.
- **A precomputed read model or cache.** A second source of truth that can drift from the
  trace; the stored trace and the oracle's verdict stay the only ones.
- **Allow any configured provider per run.** A click could then start a paid API or spend a
  free quota. The owner's flag keeps that decision in configuration.
- **Let a client name a recording by path.** A path can point anywhere (`../../`), and a file
  can change after it was checked; a manifest id names pinned bytes.
