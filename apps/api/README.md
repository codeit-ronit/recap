# `apps/api` — FastAPI backend

```bash
uvicorn apps.api.main:app --reload        # http://127.0.0.1:8000  (docs at /docs)
```

The API is a thin layer over `recap.runtime.Runtime`. Incidents run as background asyncio
tasks (`service.py: RunManager`); their trace events are stored in SQLite and also pushed to
an in-process hub that powers the live server-sent-events stream.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + environment `label`: `SIMULATED LOCAL ENVIRONMENT · V1` for a simulated environment, otherwise `LIVE ENVIRONMENT (<kind>) · V1` |
| GET | `/system` | which implementation is plugged into each interface (environment adapter, transport, provider, retriever, router, policy, verifier, oracle, sinks) |
| GET | `/scenarios` | available scenarios (ground-truth fields are for evaluation only; agents never see them) |
| POST | `/incidents/run` | `{scenario_id, config}` → starts a run, returns `run_id` (202) |
| GET | `/runs`, `/runs/{id}` | run summaries / full checkpoint + evidence + `status` (below) |
| GET | `/runs/{id}/events` | stored trace events (`?after_seq=`) |
| GET | `/runs/{id}/stream` | **SSE**: replays stored events; for a run this server is running, then live ones until `run.summary` |
| GET | `/approvals/pending` | proposals waiting for a human |
| POST | `/approvals/{proposal_id}` | `{decision, proposal_digest, actor}` — approve/reject ONE exact proposal; wrong digest, expired or already-decided → 409 |
| GET | `/memory` | experience episodes (`?partition=demo\|train`) |
| GET | `/knowledge/search?q=` | BM25 search with citations, parsed action hints and injection flags |
| GET | `/knowledge/docs` | all knowledge chunks |
| POST | `/evals/run` | `{configs?}` → start the evaluation protocol in the background; an unknown configuration name → 400 |
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

- There is **no endpoint that executes an action directly**. Clients can start simulated runs
  (optionally with the simulation auto-approver) and evaluations, and approve or reject one
  exact proposal. Approving only records a decision bound to that proposal's digest; the
  controller executes it through the safety gateway.
- `safety_enabled: false` is refused by the API (the simulation-only ablation runs only in the
  evaluation runner).
- A per-run `provider` other than the server's default is refused (400): the provider is
  server configuration, so a client cannot switch on the paid LLM provider.
- Runs use the default "manual" approval mode unless the request sets `"approval_mode": "auto"`
  (the simulation auto-approver, allowed only against a simulated environment).
