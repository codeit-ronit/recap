# `apps/api` — FastAPI backend

```bash
uvicorn apps.api.main:app --reload        # http://127.0.0.1:8000  (docs at /docs)
```

The API is a thin layer over `recap.runtime.Runtime`. Incidents run as background asyncio
tasks (`service.py: RunManager`), or in worker processes with the Redis backend (M6, below);
their trace events are stored in SQLite, and a run this process executes also pushes them to
an in-process hub that powers the live server-sent-events stream.

| File | What it does |
|---|---|
| [main.py](main.py) | the endpoints: they validate a request and call the service, nothing more |
| [service.py](service.py) | `RunManager` (runs and evaluations in the background, or the Redis queue), `check_run_request` (what a client may start), `RunReader` (reads what runs persisted, and the published runs) |
| [views.py](views.py) | builds every typed response from stored run records, evidence and trace events: pure functions, no database access |
| [dto.py](dto.py) | the typed response contracts, schema `recap-api-v1` ([ADR 0027](../../docs/decisions/0027-a-typed-read-only-api-over-the-run-records.md)) |
| [auth.py](auth.py) | the operator token, the host check and the origin check |
| [metrics.py](metrics.py) | the optional Prometheus metrics behind `GET /metrics` (`RECAP_METRICS=1`), bounded labels only |

## Signing in (operator token)

Everything that changes state needs `Authorization: Bearer <operator token>`
([auth.py](auth.py)). On first start the server generates a token, saves it to
`data/operator-token` (readable by your user only) and prints it once; paste it into the
dashboard's **Sign in** box (top right). Set `RECAP_OPERATOR_TOKEN` to choose your own and
`RECAP_OPERATOR_NAME` for the name decisions are recorded under (`operator:<name>`).
Reads, including the live event stream, need no token. The server answers only requests
addressed to `localhost`, `127.0.0.1` or `[::1]` (`RECAP_ALLOWED_HOSTS` to change that; other
names get 400, which stops DNS-rebinding attacks), and refuses a state-changing request from
a browser page on another host (403).

```bash
curl -H "Authorization: Bearer $(cat data/operator-token)" http://127.0.0.1:8000/auth/session
```

In the table, **operator** marks the six routes that need the token. Every other route only
reads and needs none.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness: `status`, `version`, `environment` (its kind), the default `provider`, the `label` (`SIMULATED LOCAL ENVIRONMENT · V1` for a simulated environment, otherwise `LIVE ENVIRONMENT (<kind>) · V1`), `execution_backend` (`inprocess` or `redis`) and `queue_reachable` (null in process) |
| GET | `/system` | which implementation is plugged into each interface and its real status (`components`, `environment_adapters`: Kind and SREGym are implemented since M5, and operational only while configured), `features` (default retrieval and hybrid readiness, memory partitions and who reads and writes them, replay fixtures, which providers a client may pick, writes need the operator token) and `versions` |
| GET | `/providers` | every configured decision provider: its class (`provider_class`), model, status (`ready`, `needs_setup`, `stopped`, `disabled`, `unavailable`), configured `limits` (free-tier caps included), honest `limitations`, whether a client may pick it for one run (`selectable`, `selection_rule`), whether a key is present (never its value) and usage. It runs no inference and calls no hosted API (it only asks a local Ollama server which models are installed) |
| POST | `/environment/check` | **operator** · can the configured target be reached right now? A real cluster's adapter runs its own preflight (each kubeconfig's server and CA fingerprint, the version skew, the monitoring clock), bounded to 20 s; nothing changes and no run starts. `{checked_at, environment_kind, simulated, ok, checks: [{name, ok, detail}]}` |
| GET | `/metrics` | only with `RECAP_METRICS=1` (otherwise 404): this server's operational counters in the Prometheus text format (requests, incidents, tool calls, model requests with their time and tokens, actions, approval waits, recovery verdicts; with the Redis backend also the queue: depth, wait, busy workers, jobs), with bounded labels only; each worker serves its own runs' metrics on `RECAP_WORKER_METRICS_PORT` ([../worker/metrics.py](../worker/metrics.py)), never an id, a prompt, model text or a credential ([metrics.py](metrics.py)); not in the OpenAPI schema |
| GET | `/scenarios` | available scenarios (ground-truth fields are for evaluation only; agents never see them) |
| GET | `/replays` | the committed recordings of real model runs (`fixtures/recordings/MANIFEST.json`): model, prompt version, agents, who approved the original fix, a short SHA-256, and whether each can be replayed here (`integrity`: `intact`, `altered` or `missing`; `launchable`, and why not) |
| GET | `/auth/session` | **operator** · who the token belongs to (`{operator, actor}`); 401 without a valid token |
| POST | `/incidents/run` | **operator** · `{scenario_id, config}` → starts a run (202): `{run_id, status, provider, provider_class, replay_fixture, labels, job_id, queue_position, duplicate}`; see "Starting a run" below. An `Idempotency-Key` header (1-128 of `A-Za-z0-9._:-`; a malformed one → 400) names one submission: repeated with the same request it returns that run (`duplicate: true`) and starts nothing; with another request 422. A local Ollama model on this machine without the free memory it needs → 503 ([local_memory.py](../../recap/providers/local_memory.py)). With the Redis backend (below) the run is `queued`; a full queue → 429 with `Retry-After`, Redis unreachable → 503 |
| POST | `/runs/{id}/cancel` | **operator** · cancels a run (202, `status: cancelling`); its pending approval request expires; 404 unknown, 409 not running here, 409 for a published run (read-only). With the Redis backend a queued run is taken off the queue (`status: cancelled`: nothing ran), a worker's run stops at the worker's next heartbeat, and Redis unreachable → 503 |
| GET | `/queue` | how runs execute (`backend`: `inprocess` or `redis`) and, with Redis, the queue's depth and capacity, the workers (alive or not, from their heartbeats), the newest jobs and the queue's counters; read-only. `reachable: false` when Redis does not answer (runs, traces and approvals stay readable) |
| GET | `/jobs/{job_id}` | one job of the Redis queue: its status, its attempts (each a run), its worker, why it ended (`category`); 404 unknown (and always without the Redis backend), 503 Redis unreachable |
| GET | `/published` | the real-cluster runs published with the repository (`evals/live/MANIFEST.json`), each shown only after its files pass `evals/published.py`'s checks, as run summaries with their `published` provenance (folder, target, a person's title and notes, SREGym's grader verdict kept apart); the real-cluster evaluation records without a published trace; every refused entry with the reason; what the files do not hold. Offline: no cluster, no model (see "Published runs" below) |
| GET | `/runs` | this server's run summaries (published runs are listed by `/published`), newest first, as a JSON array; filters `provider_class`, `provider` (provider id or model), `scenario`, `state`, `environment`, `label`; pages with `limit` (1-1000, default 50) and `offset`; the number of matching runs is in the `X-Total-Count` header |
| GET | `/runs/{id}` | `{schema_version, status, summary, record, evidence, error, job}`: the typed summary plus the stored checkpoint and evidence as before; `job` with the Redis backend. A published run is served when this server holds no run with that id |
| GET | `/runs/{id}/timeline` | one page of the run's trace as typed entries (`after_seq`, `limit` 1-2000, filters `agent`, `round`, `kind`, `tool`), plus the run's agent lanes, rounds (with overlap), counts and retries; `next_after_seq` is the next page's cursor |
| GET | `/runs/{id}/evidence` | the evidence registry with trust classes, every claim with its citations, every verdict (rule lines, basis evidence, disagreements, leads), proposals, policy verdicts on fixes, proposal checks, injection flags and evidence ids cited but never observed (a published run's registry contents were not published, and the view says so) |
| GET | `/runs/{id}/retrieval` | runbook searches (stage, who wrote the query, BM25 or hybrid, results with ranks, signals and who cited them), refused agent searches, memory searches, reuse verdicts and what was written |
| GET | `/runs/{id}/usage` | per model: requests, the server's own token counts (null when it reported none), summed request time, server time and the difference; RECAP's estimated context tokens (labeled an estimate); the budget; the run's own cost line |
| GET | `/runs/{id}/events` | stored trace events (`?after_seq=`), or a published run's recorded ones |
| GET | `/runs/{id}/stream` | **SSE**: replays stored events; for a run this server is running, then live ones until `run.summary`; a worker's run (Redis backend) is followed from the trace the worker stores, from before it starts until its `run.summary`; a published run's recorded trace, then the end; resumes after `Last-Event-ID` or `after_seq` (below) |
| GET | `/approvals/pending` | proposals waiting for a human |
| POST | `/approvals/{proposal_id}` | **operator** · `{decision, proposal_digest, confirm_target?}` — approve/reject ONE exact proposal; 404 when no request has that id; on a target that is not simulated, approving needs `confirm_target`, the proposal's target service typed exactly (otherwise 400 and nothing is decided), and the decision log records how and when it came; the approver recorded is the signed-in operator (an `actor` field is refused with 422). A rejection also revokes an approval that was not used yet; approving after a rejection, deciding an expired request, or rejecting after the gateway used the approval (right before sending the write) → 409 |
| GET | `/approvals/{proposal_id}/events` | the proposal's append-only decision log (requested, approved, rejected, revoked, expired, consumed, refused) |
| GET | `/approvals/events` | the newest decision-log entries across all proposals (`?limit=`, 1-500, default 50) |
| GET | `/memory` | experience episodes (`?partition=demo\|train\|live\|test`) |
| GET | `/knowledge/search?q=` | BM25 search (`q` of 2 characters or more, `k` results, default 4) with citations, parsed action hints and injection flags |
| GET | `/knowledge/docs` | all knowledge chunks |
| POST | `/evals/run` | **operator** · `{configs?}` → start the scripted evaluation protocol in the background, one at a time (`status`: `started` or `already running`); an unknown configuration name → 400. The answer names the report folder repo-relative, never by a local path |
| GET | `/evals/results` | `{running, progress, report, error}` for the evaluation this server started (below) |
| GET | `/evals` | evaluation batches, each of ONE mode (scripted regression, offline replay, real LLM, real Kubernetes), published (`evals/reports/batches/`) or local (`<data>/evals/batches/`, and `evals/reports/runs/` for one this server ran), with counts per outcome and failure category and a benchmark grader's verdicts kept apart; and the four modes, all `implemented` since M5 |
| GET | `/evals/components` | the published component evaluations (`evals/reports/components/`, newest first): retrieval, memory, routing, budgets, the verifier and safety, each measured on its own dataset or on one batch's own runs ([evals/components.py](../../evals/components.py)), never an end-to-end success rate |
| GET | `/evals/{batch_id}` | one batch with every record (a batch is found by its id, never by a path); since M6 W4 also its configurations, its one-switch comparisons as measured on its own records when it was made ([evals/compare.py](../../evals/compare.py)) and its scenario sets, empty for older batches |
| GET | `/docs`, `/redoc`, `/openapi.json` | FastAPI's own: the interactive documentation and the OpenAPI schema, with every typed field (`/metrics` is left out) |
| GET | `/` | the built dashboard (`apps/dashboard/dist`), mounted only when that folder exists when the server starts |

## The typed read contracts

The typed read endpoints (system, providers, replays, runs and the four run views, the queue and
its jobs, published runs, evaluation batches and component evaluations), and the answers of
`POST /incidents/run` and `POST /environment/check`, return models from [dto.py](dto.py), so
`GET /openapi.json` (and the page at `/docs`) documents every field. Four rules hold for them:

- **Versioned.** Every response carries the header `X-RECAP-API-Schema: recap-api-v1`, and every
  typed JSON object also has `schema_version`. `GET /runs` and `GET /providers` stay JSON arrays,
  as the dashboard has always read them. Adding a field keeps the schema name; renaming or
  removing one needs a new name.
- **Only what was recorded.** Each value is copied, counted or matched from the run's stored
  checkpoint, its trace events, its evidence and the oracle's verdict. A field the record
  lacks is `null` and named in `not_recorded` (an old run without a code version, a run whose
  trace is missing, a request whose server reported no time). Tokens a server did not report
  stay `null`, never an estimate; RECAP's own estimates are labeled `estimate: true`.
- **Who wrote it.** Text from a model, a tool, an alert or a runbook is data: claims and
  summaries name their `author` (`model`, `replayed_model`, `scripted_policy`), evidence names
  its `trust_class`, and a timeline entry says `untrusted_text`. The verifier's rule lines are
  RECAP's own text. A replay of a committed fixture is labeled `RECORDED REAL-LLM RUN`; any
  other replay only `REPLAY`; a scripted run `SCRIPTED BASELINE (NOT AN LLM)`.
- **Nothing secret.** No response carries a key, the operator token, a signed capability or a
  local folder: the data folder becomes `data`, the checkout folder disappears (paths are
  repo-relative) and the home folder becomes `~` (`views.LocalPaths`).

A run's `outcome` sums up how it ended from its recorded state, last action and note:
`in_progress`, `resolved` (the oracle verified the recovery), `failed`, `error`, `cancelled`,
`approval_rejected`, `approval_expired`, `safety_blocked`, `budget_exhausted`,
`quota_deferred`, or, with the Redis backend, `interrupted` or `needs_reconciliation` (below).

**Time, honestly.** A model request has `duration_ms` (RECAP's own measurement, from sending it
to the answer) and, when the server reports one, `server_ms` (Ollama's `total_duration`). A local
Ollama server may answer one request at a time, so when two agents run in parallel their turns
overlap while one agent's request can wait inside the server for the other's. Ollama counts
that wait in its `total_duration`, from the moment it received the request: the wait is inside
both numbers, and `outside_server_ms` (the difference) is connection and transfer time, not
queueing. The committed golden recording shows it: its 14 chat requests took 734.3 s as RECAP
measured them and 733.4 s as Ollama counted them, while Ollama's compute (loading, reading the
prompt, writing the answer) adds up to 474.3 s; about 259 s inside Ollama were not compute.
Since M4 a request also records that compute (`server_compute_ms`), and the API reports
`server_wait_ms` (`server_ms` minus the compute: the time the request waited inside the
server), null when the server did not report all of it. The golden run's own trace predates
this; a replay of its recording computes both from the recorded answers.
In a replay, request times are the original run's, while agent turn times (lanes, a round's
wall time and overlap) are the replay's own.

## Execution backends (M6)

By default the API runs each incident as a task in its own process. With
`RECAP_EXECUTION_BACKEND=redis` and `RECAP_REDIS_URL=redis://host:6379/0`, `POST /incidents/run`
queues it in Redis and worker processes (`python -m apps.worker`) execute it through the same
harness ([recap/workers/](../../recap/workers/), [ADR 0034](../../docs/decisions/0034-durable-workers-redis-coordinates-the-run-records-decide.md)).
Nothing connects to Redis until a request needs it: the API starts without Redis, and only what
needs the queue answers 503 while it is away. A run's `status` then also says `queued`,
`interrupted` (its worker stopped and no worker has settled it yet) or `cancelled` (taken off
the queue before it started); `GET /runs/{id}` adds the run's `job`. A run closed after its
worker stopped has `record.interruption`, and its outcome is `interrupted` (no fix reached the
target; it may have run again as `retried_as`) or `needs_reconciliation` (a fix may have
reached the target: never retried, a person checks the target).

Settings: `RECAP_QUEUE_CAPACITY` (20), `RECAP_JOB_MAX_ATTEMPTS` (3),
`RECAP_WORKER_HEARTBEAT_SECONDS` (5), `RECAP_WORKER_HEARTBEAT_TTL_SECONDS` (20),
`RECAP_REDIS_PREFIX` (`recap`); for a worker, `RECAP_WORKER_CONCURRENCY` (1; always 1 on a live
target) and `RECAP_WORKER_GRACE_SECONDS` (25). The Docker Compose setup runs all of this:
[infra/compose/README.md](../../infra/compose/README.md).

## Published runs (M6)

`GET /published` lists the real-cluster runs that ship with the repository: an allowlist,
[evals/live/MANIFEST.json](../../evals/live/MANIFEST.json), pins each run's four files by
SHA-256 and size, and [evals/published.py](../../evals/published.py) shows a run only when the
files match their pins, validate as RECAP's records and trace events, and agree with each other
and with the run's evaluation record; a run that fails is refused whole, with the reason
([ADR 0033](../../docs/decisions/0033-published-real-cluster-runs-read-only-behind-the-same-views.md)).
When this server holds no run with that id, the run views (`/runs/{id}` and its `timeline`,
`evidence`, `retrieval`, `usage`, `events` and `stream`) serve the published run with the
status `published`. It is read-only: its stream replays the recorded trace and ends, cancelling
it answers 409, its approvals are never loaded into the approval store (so they cannot be
decided again or reused), and its recording is not a replay fixture. This server's own copy of
a run wins over a published one.

## Starting a run: which provider

`POST /incidents/run` refuses `safety_enabled: false` (400), an unknown scenario (404), and on a
target that is not simulated the simulation auto-approver (`approval_mode: "auto"`, 400): a
person approves every live write. The `config.provider` a client may name for ONE run:

| Choice | Allowed? |
|---|---|
| none, or the server's default | yes: the owner configured it |
| the scripted baseline (`deterministic`) | yes: not an LLM, offline, $0 |
| `replay` | only with `replay_fixture`: the id of a committed recording (`GET /replays`). Its bytes must match the manifest's SHA-256, it must have been recorded with the prompts this server renders, and the scenario must be the recording's. Routing, memory and retrieval come from the manifest entry; a request that sets one of them differently is refused. A client never names a path |
| a model provider the owner marked `api_selectable: true` in [configs/providers.yaml](../../configs/providers.yaml) | yes (the local `ollama` provider is marked: $0) |
| any other provider (`groq` unless the owner marks it, a paid provider whatever its flag, an unknown name) | no: 400, with the reason |

A replay calls no model: its answers are the recorded run's, and it asks the operator to
approve the fix again. Its run summary names the fixture, the recording it really replayed and
the folder where the original run was exported.

## The event stream

`GET /runs/{id}/stream` sends each stored event once, as `id: <seq>`, `event: trace` and the
event as `data`, then (for a run this server or a worker is running) each new event, until
`run.summary`. A client that reconnects sends the last id it received as `Last-Event-ID`
(a browser's `EventSource` does this by itself) or passes `?after_seq=`; the stream resumes
right after the later of the two, so no event arrives twice.

## Run status

`GET /runs/{id}` reports where a run stands, as far as this server process can tell. Live
events exist only in the process that runs the incident; a worker's run is followed from the
trace it stores.

| `status` | Meaning |
|---|---|
| `running` | this server is running it, or (Redis backend) a worker whose heartbeat is current; `/stream` follows its events |
| `finished` | it ended: a run this server started, or a stored run in a terminal state |
| `error` | a run this server started crashed, or a worker's run failed before it recorded anything; `error` says why |
| `queued` | (Redis backend) waiting for a worker: no record yet, and `job` says where it is in the queue |
| `interrupted` | (Redis backend) its worker stopped mid-run and no worker has settled it yet |
| `cancelled` | (Redis backend) taken off the queue before it started: nothing ran |
| `external` | started by another process (the CLI, or an earlier server that stopped) and not in a terminal state, e.g. a CLI run waiting for approval. Its stored record and events are served, but `/stream` ends after replaying them; poll `/runs/{id}/events?after_seq=` for more |
| `published` | a real-cluster run published with the repository (above): historical, read-only |
| `unknown` | no such run (the endpoint answers 404) |

## Evaluations

`GET /evals/results` reports on the evaluation this server started: `running: true` with the
latest progress lines while it runs, then either its `report` or, if it failed, `report: null`
with the `error`. Only before this server starts an evaluation does it return the committed
report in `evals/reports/latest/`, so a failed evaluation is never hidden behind an older one.
The runner refuses any provider other than the scripted default policy (that refusal is
reported as the `error`). `GET /evals` lists the evaluation batches ([evals/records.py](../../evals/records.py)):
a batch holds runs of one mode only, and a run counts as a success only with the oracle's
verified recovery. A benchmark grader's verdicts (SREGym's) are counted apart, never added to
the successes. Since M6 W4, `GET /evals/{batch_id}` also returns a batch's one-switch
comparisons, kept with the batch when it was made, and `GET /evals/components` serves the
component evaluations
([ADR 0035](../../docs/decisions/0035-one-switch-comparisons-separate-component-datasets-nothing-pooled.md),
[docs/evaluation.md](../../docs/evaluation.md) section 13).

## Safety notes

- State-changing endpoints are **operator-only** (bearer token, constant-time check), and the
  approver identity comes from the credential, never from the request body. This is a
  single-operator scheme for a local prototype: a shared deployment needs real user accounts,
  roles and TLS.
- There is **no endpoint that executes an action directly**. Clients can start runs on the
  server's target (the simulation auto-approver only on a simulated one) and evaluations,
  cancel a run, check that the target can be reached, and approve or reject one exact proposal.
  Approving only records a decision bound to that proposal's digest; the controller executes it
  through the safety gateway. A published run can never be acted on.
- `safety_enabled: false` is refused by the API (the simulation-only ablation runs only in the
  evaluation runner).
- A client can never switch on a paid provider, nor start real inference or spend a free quota
  the owner did not allow from the API (see "Starting a run"). To make another provider the
  default for every run, start the server with it, for example
  `RECAP_PROVIDER=ollama uvicorn apps.api.main:app` (local), or for Groq's free plan also
  export `GROQ_API_KEY`, `RECAP_GROQ_FREE_PLAN_CONFIRMED_ON` and
  `RECAP_ALLOW_LIVE_FREE_INFERENCE=1` in that server's shell
  ([recap/providers/README.md](../../recap/providers/README.md)).
- Runs use the default "manual" approval mode unless the request sets `"approval_mode": "auto"`
  (the simulation auto-approver, allowed only against a simulated environment).
- Reading never runs inference: `GET /providers` only asks a local Ollama server which models
  are installed, and only an operator's `POST /incidents/run` starts a run.
