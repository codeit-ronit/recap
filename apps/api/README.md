# `apps/api` — FastAPI backend

```bash
uvicorn apps.api.main:app --reload        # http://127.0.0.1:8000  (docs at /docs)
```

The API is a thin layer over `recap.runtime.Runtime`. Incidents run as background asyncio
tasks (`service.py: RunManager`); their trace events are stored in SQLite and also pushed to
an in-process hub that powers the live server-sent-events stream.

| File | What it does |
|---|---|
| [main.py](main.py) | the endpoints: they validate a request and call the service, nothing more |
| [service.py](service.py) | `RunManager` (runs and evaluations in the background), `check_run_request` (what a client may start), `RunReader` (reads what runs persisted) |
| [views.py](views.py) | builds every typed response from stored run records, evidence and trace events: pure functions, no database access |
| [dto.py](dto.py) | the typed response contracts, schema `recap-api-v1` ([ADR 0027](../../docs/decisions/0027-a-typed-read-only-api-over-the-run-records.md)) |
| [auth.py](auth.py) | the operator token, the host check and the origin check |

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

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + environment `label`: `SIMULATED LOCAL ENVIRONMENT · V1` for a simulated environment, otherwise `LIVE ENVIRONMENT (<kind>) · V1` |
| GET | `/system` | which implementation is plugged into each interface and its real status (`components`, `environment_adapters`: SREGym and Kind are `planned` for M5, never operational), `features` (default retrieval and hybrid readiness, memory partitions and who reads and writes them, replay fixtures, which providers a client may pick, writes need the operator token) and `versions` |
| GET | `/providers` | every configured decision provider: its class (`provider_class`), model, status (`ready`, `needs_setup`, `stopped`, `disabled`, `unavailable`), configured `limits` (free-tier caps included), honest `limitations`, whether a client may pick it for one run (`selectable`, `selection_rule`), whether a key is present (never its value) and usage. It runs no inference and calls no hosted API (it only asks a local Ollama server which models are installed) |
| GET | `/scenarios` | available scenarios (ground-truth fields are for evaluation only; agents never see them) |
| GET | `/replays` | the committed recordings of real model runs (`fixtures/recordings/MANIFEST.json`): model, prompt version, agents, who approved the original fix, a short SHA-256, and whether each can be replayed here (`integrity`: `intact`, `altered` or `missing`; `launchable`, and why not) |
| GET | `/auth/session` | **operator** · who the token belongs to (`{operator, actor}`); 401 without a valid token |
| POST | `/incidents/run` | **operator** · `{scenario_id, config}` → starts a run (202): `{run_id, provider, provider_class, replay_fixture, labels}`; see "Starting a run" below |
| POST | `/runs/{id}/cancel` | **operator** · cancels a run this server is running (202); its pending approval request expires; 404 unknown, 409 not running here |
| GET | `/runs` | run summaries, newest first, as a JSON array; filters `provider_class`, `provider` (provider id or model), `scenario`, `state`, `environment`, `label`; pages with `limit` (1-1000, default 50) and `offset`; the number of matching runs is in the `X-Total-Count` header |
| GET | `/runs/{id}` | `{schema_version, status, summary, record, evidence, error}`: the typed summary plus the stored checkpoint and evidence as before |
| GET | `/runs/{id}/timeline` | one page of the run's trace as typed entries (`after_seq`, `limit` 1-2000, filters `agent`, `round`, `kind`, `tool`), plus the run's agent lanes, rounds (with overlap), counts and retries; `next_after_seq` is the next page's cursor |
| GET | `/runs/{id}/evidence` | the evidence registry with trust classes, every claim with its citations, every verdict (rule lines, basis evidence, disagreements, leads), proposals, policy verdicts on fixes, proposal checks, injection flags and evidence ids cited but never observed |
| GET | `/runs/{id}/retrieval` | runbook searches (stage, who wrote the query, BM25 or hybrid, results with ranks, signals and who cited them), refused agent searches, memory searches, reuse verdicts and what was written |
| GET | `/runs/{id}/usage` | per model: requests, the server's own token counts (null when it reported none), summed request time, server time and the difference; RECAP's estimated context tokens (labeled an estimate); the budget; the run's own cost line |
| GET | `/runs/{id}/events` | stored trace events (`?after_seq=`) |
| GET | `/runs/{id}/stream` | **SSE**: replays stored events; for a run this server is running, then live ones until `run.summary`; resumes after `Last-Event-ID` or `after_seq` (below) |
| GET | `/approvals/pending` | proposals waiting for a human |
| POST | `/approvals/{proposal_id}` | **operator** · `{decision, proposal_digest}` — approve/reject ONE exact proposal; the approver recorded is the signed-in operator (an `actor` field is refused with 422). A rejection also revokes an approval that was not used yet; approving after a rejection, deciding an expired request, or rejecting after the gateway used the approval (right before sending the write) → 409 |
| GET | `/approvals/{proposal_id}/events` | the proposal's append-only decision log (requested, approved, rejected, revoked, expired, consumed, refused) |
| GET | `/approvals/events` | the newest decision-log entries across all proposals (`?limit=`) |
| GET | `/memory` | experience episodes (`?partition=demo\|train\|live\|test`) |
| GET | `/knowledge/search?q=` | BM25 search with citations, parsed action hints and injection flags |
| GET | `/knowledge/docs` | all knowledge chunks |
| POST | `/evals/run` | **operator** · `{configs?}` → start the evaluation protocol in the background; an unknown configuration name → 400. The answer names the report folder repo-relative, never by a local path |
| GET | `/evals/results` | `{running, progress, report, error}` for the evaluation this server started (below) |
| GET | `/evals` | evaluation batches, each of ONE mode (scripted regression, offline replay, real LLM), published (`evals/reports/batches/`) or local (`<data>/evals/batches/`), with counts per outcome and failure category; and the four modes, the Kubernetes one `planned` (M5) |
| GET | `/evals/{batch_id}` | one batch with every record (a batch is found by its id, never by a path) |

## The typed read contracts

The new and extended read endpoints (system, providers, runs, the four run views, replays,
evaluation batches) return typed models from [dto.py](dto.py), so `GET /openapi.json` (and
the page at `/docs`) documents every field. Four rules hold for them:

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
`approval_rejected`, `approval_expired`, `safety_blocked`, `budget_exhausted` or
`quota_deferred`.

**Time, honestly.** A model request has `duration_ms` (RECAP's own measurement, from sending it
to the answer) and, when the server reports one, `server_ms` (Ollama's `total_duration`). A local
Ollama server may answer one request at a time, so when two agents run in parallel their turns
overlap while one agent's request can wait inside the server for the other's. Ollama counts
that wait in its `total_duration`, from the moment it received the request: the wait is inside
both numbers, and `outside_server_ms` (the difference) is connection and transfer time, not
queueing. The committed golden recording shows it: its 14 chat requests took 734.3 s as RECAP
measured them and 733.4 s as Ollama counted them, while Ollama's compute (loading, reading the
prompt, writing the answer) adds up to 474.3 s; about 259 s inside Ollama were not compute. The
trace keeps only Ollama's total, not its compute time, so the API cannot show that wait yet.
In a replay, request times are the original run's, while agent turn times (lanes, a round's
wall time and overlap) are the replay's own.

## Starting a run: which provider

`POST /incidents/run` refuses `safety_enabled: false` (400) and an unknown scenario (404). The
`config.provider` a client may name for ONE run:

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
event as `data`, then (for a run this server is running) each live event, until
`run.summary`. A client that reconnects sends the last id it received as `Last-Event-ID`
(a browser's `EventSource` does this by itself) or passes `?after_seq=`; the stream resumes
right after the later of the two, so no event arrives twice.

## Run status

`GET /runs/{id}` reports where a run stands, as far as this server process can tell. Live
events exist only in the process that runs the incident.

| `status` | Meaning |
|---|---|
| `running` | this server is running it; `/stream` follows its live events |
| `finished` | it ended: a run this server started, or a stored run in a terminal state |
| `error` | a run this server started crashed; `error` says why |
| `external` | started by another process (the CLI, or an earlier server that stopped) and not in a terminal state, e.g. a CLI run waiting for approval. Its stored record and events are served, but `/stream` ends after replaying them; poll `/runs/{id}/events?after_seq=` for more |
| `unknown` | no such run (the endpoint answers 404) |

## Evaluations

`GET /evals/results` reports on the evaluation this server started: `running: true` with the
latest progress lines while it runs, then either its `report` or, if it failed, `report: null`
with the `error`. Only before this server starts an evaluation does it return the committed
report in `evals/reports/latest/`, so a failed evaluation is never hidden behind an older one.
The runner refuses any provider other than the scripted default policy (that refusal is
reported as the `error`). `GET /evals` lists the evaluation batches ([evals/records.py](../../evals/records.py)):
a batch holds runs of one mode only, and a run counts as a success only with the oracle's
verified recovery.

## Safety notes

- State-changing endpoints are **operator-only** (bearer token, constant-time check), and the
  approver identity comes from the credential, never from the request body. This is a
  single-operator scheme for a local prototype: a shared deployment needs real user accounts,
  roles and TLS.
- There is **no endpoint that executes an action directly**. Clients can start simulated runs
  (optionally with the simulation auto-approver) and evaluations, and approve or reject one
  exact proposal. Approving only records a decision bound to that proposal's digest; the
  controller executes it through the safety gateway.
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
