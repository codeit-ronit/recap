# `apps/api` — FastAPI backend

```bash
uvicorn apps.api.main:app --reload        # http://127.0.0.1:8000  (docs at /docs)
```

The API is a thin layer over `recap.runtime.Runtime`. Incidents run as background asyncio
tasks (`service.py: RunManager`); their trace events are stored in SQLite and also pushed to
an in-process hub that powers the live server-sent-events stream.

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
| GET | `/system` | which implementation is plugged into each interface (environment adapter, transport, provider, retriever, router, policy, verifier, oracle, sinks) |
| GET | `/providers` | every configured decision provider and whether it could run now: origin, model, status (`ready`, `needs_setup`, `stopped`, `disabled`, `unavailable`), whether a key is present (never its value), caps and usage. It runs no inference and calls no hosted API (it only asks a local Ollama server which models are installed) |
| GET | `/scenarios` | available scenarios (ground-truth fields are for evaluation only; agents never see them) |
| GET | `/auth/session` | **operator** · who the token belongs to (`{operator, actor}`); 401 without a valid token |
| POST | `/incidents/run` | **operator** · `{scenario_id, config}` → starts a run, returns `run_id` (202) |
| POST | `/runs/{id}/cancel` | **operator** · cancels a run this server is running (202); its pending approval request expires; 404 unknown, 409 not running here |
| GET | `/runs`, `/runs/{id}` | run summaries / full checkpoint + evidence + `status` (below) |
| GET | `/runs/{id}/events` | stored trace events (`?after_seq=`) |
| GET | `/runs/{id}/stream` | **SSE**: replays stored events; for a run this server is running, then live ones until `run.summary` |
| GET | `/approvals/pending` | proposals waiting for a human |
| POST | `/approvals/{proposal_id}` | **operator** · `{decision, proposal_digest}` — approve/reject ONE exact proposal; the approver recorded is the signed-in operator (an `actor` field is refused with 422). A rejection also revokes an approval that was not used yet; approving after a rejection, deciding an expired request, or rejecting after the gateway used the approval (right before sending the write) → 409 |
| GET | `/approvals/{proposal_id}/events` | the proposal's append-only decision log (requested, approved, rejected, revoked, expired, consumed, refused) |
| GET | `/approvals/events` | the newest decision-log entries across all proposals (`?limit=`) |
| GET | `/memory` | experience episodes (`?partition=demo\|train`) |
| GET | `/knowledge/search?q=` | BM25 search with citations, parsed action hints and injection flags |
| GET | `/knowledge/docs` | all knowledge chunks |
| POST | `/evals/run` | **operator** · `{configs?}` → start the evaluation protocol in the background; an unknown configuration name → 400 |
| GET | `/evals/results` | `{running, progress, report, error}` for the evaluation this server started (below) |

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
reported as the `error`).

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
- A per-run `provider` other than the server's default is refused (400): the provider is
  server configuration, so a client cannot switch on the paid LLM provider or spend a free
  quota. To run real models from the dashboard, start the server with one, for example
  `RECAP_PROVIDER=ollama uvicorn apps.api.main:app` (local), or for Groq's free plan also
  export `GROQ_API_KEY`, `RECAP_GROQ_FREE_PLAN_CONFIRMED_ON` and
  `RECAP_ALLOW_LIVE_FREE_INFERENCE=1` in that server's shell
  ([recap/providers/README.md](../../recap/providers/README.md)).
- Runs use the default "manual" approval mode unless the request sets `"approval_mode": "auto"`
  (the simulation auto-approver, allowed only against a simulated environment).
