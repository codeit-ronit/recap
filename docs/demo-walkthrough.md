# Demo walkthrough: replay a recorded real two-agent incident

This is a 90 to 180 second tour of RECAP's golden demo for a reviewer: a **real** run of the
`qwen3:8b` model, recorded on 2026-10-25, **replayed** offline through RECAP's whole
incident-response path against the **simulated** online shop. You need no model, no API key
and no network once RECAP is installed. Ollama may be stopped or not installed at all.

> **A replay is not live inference.** No language model runs during this demo. Every model
> answer you see was written by `qwen3:8b` in the original run and is read back from a
> recording (`fixtures/recordings/ollama-qwen3-8b-unfamiliar-parallel.jsonl`). Everything
> else runs now, for real: the controller, the MCP tool server, the safety gateway, the
> simulated shop, the evidence verifier, your approval, the signed write and the recovery
> oracle. The target is a simulation, never a real cluster. RECAP labels such a run
> `REPLAY` and, because the recording is a committed one, `RECORDED REAL-LLM RUN`, next to
> `SIMULATED ENVIRONMENT`.

## What the recording is

| | |
|---|---|
| Manifest id | `qwen3-8b-unfamiliar-parallel` ([fixtures/recordings/MANIFEST.json](../fixtures/recordings/MANIFEST.json)) |
| Original run | `20261025-121828-ollama-6a5cfd`, exported in [evals/live/2026-10-25-qwen3-8b-human-approved-unfamiliar-prompts-v4.7/](../evals/live/2026-10-25-qwen3-8b-human-approved-unfamiliar-prompts-v4.7/README.md) (the same bytes) |
| Model | `qwen3:8b` via Ollama 0.34.4 on a 16 GB M3 MacBook Air, prompts-v4.7, 14 model requests, 52,585 input / 2,394 output tokens, about 11 minutes live |
| Incident | `m3-inventory-leak-behind-frontend`, one of the two held-out incidents added in M3 ([ADR 0025](decisions/0025-held-out-unfamiliar-and-adversarial-incidents.md)): shoppers see slow pages and a stuck basket; the alert names frontend, two calls away from the cause (a memory leak in inventory) |
| Agents | two specialists in parallel, two rounds (telemetry 6 reads, infrastructure 10) |
| Original approval | the owner, at an interactive terminal, 162.5 s after the request |

It is one run: it shows that the path works end to end with a real model's decisions, not
how often the model is right ([docs/status.md](status.md) lists the failed runs too).

## Before you start (once, a few minutes)

Follow the Quickstart in the [README](../README.md): create the Python environment, fill a
separate demo folder with real offline runs (scripted runs and replays of the committed
recordings, so every screen has something to show), build the dashboard and start the API,
which serves the built dashboard:

```bash
python scripts/seed_demo.py --data-dir ~/recap-demo       # about a minute, no model, $0
cd apps/dashboard && npm install && npm run build && cd ../..
RECAP_DATA_DIR=~/recap-demo uvicorn apps.api.main:app     # http://127.0.0.1:8000/
```

On its first start the API prints an operator token and saves it to
`~/recap-demo/operator-token` (readable by your user only). Open http://127.0.0.1:8000/ and paste the token into **Sign
in** (top right): starting a run and approving a fix need it; looking does not. **Sign in
before you start any screen recording**, and never show the token, that file or the
terminal line where the API printed it.

## Start the replay

**In the dashboard:** on **Overview** click **Start the demo**, or open the **Incident Lab**.
Its "Replay a recorded incident" panel lists the committed recordings with their integrity
check (intact, altered or missing). Pick the two-agent recording, keep the pace on
**Watchable** (each recorded answer waits 5% of its original time, at most 15 s, so the run
takes about half a minute; **Instant** answers at once) and click **Replay**. The button
sends the request below with your operator token, and the run plays on the same page.

**The same request by hand** (useful to see exactly what the button does):

```bash
TOKEN=$(cat ~/recap-demo/operator-token)          # the server's data folder holds its token
curl -s http://127.0.0.1:8000/replays            # the committed recordings, and whether each can run

curl -s -X POST http://127.0.0.1:8000/incidents/run \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"scenario_id": "m3-inventory-leak-behind-frontend",
       "config": {"provider": "replay", "replay_fixture": "qwen3-8b-unfamiliar-parallel",
                  "approval_mode": "manual", "replay_pace": 0.05}}'
# -> {"run_id": "run-...", ...}
```

A client names the recording by its manifest id, never by a path. The server checks the
file's SHA-256 against the manifest before anything starts: a recording with one changed
byte is refused ("recording 'qwen3-8b-unfamiliar-parallel' was altered"). The replay runs
with the routing, memory and retrieval settings the original run used, because it must ask
the recording exactly what the original run asked.

To approve by hand too: `GET /approvals/pending` shows the waiting proposal (its
`proposal_id`, the exact `proposal` and its `proposal_digest`); approving names that digest,
so you can only approve exactly what you were shown:

```bash
curl -s http://127.0.0.1:8000/approvals/pending
curl -s -X POST http://127.0.0.1:8000/approvals/<proposal_id> \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"decision": "approved", "proposal_digest": "<proposal_digest>"}'
```

Keep `$TOKEN` out of anything you share (never `echo` it while recording).

**Without the dashboard** (a terminal only; you approve at the prompt):

```bash
RECAP_PROVIDER=replay \
RECAP_REPLAY_RECORDING=fixtures/recordings/ollama-qwen3-8b-unfamiliar-parallel.jsonl \
python scripts/demo.py --scenario m3-inventory-leak-behind-frontend --strategy parallel --approve ask
```

## The tour (about 150 seconds)

At the Watchable pace the investigation takes about half a minute (at the Instant pace about
2 seconds); then the run waits for you at the approval. Take your time there: the request
expires after 15 minutes. The pace changes timing only: requests are matched by what they
ask, so the run decides exactly what the recording decided.

| Time | Where | What to show | What it demonstrates |
|---|---|---|---|
| 0:00 - 0:15 | Incident Lab, the run's header | the labels `REPLAY`, `RECORDED REAL-LLM RUN`, `SIMULATED ENVIRONMENT`; the provider `replay:ollama:qwen3:8b`; the recording's id | what this run is, before anything else |
| 0:15 - 0:35 | Incident Lab, the stage stepper and the live feed | CREATED, RETRIEVING, INVESTIGATING (two rounds), PROPOSING, then AWAITING_APPROVAL; the feed shows both specialists' tool calls as they happen | the controller's lifecycle, running now |
| 0:35 - 1:05 | Trace Explorer | two lanes, telemetry and infrastructure, side by side in round 1; round 2 started from the verifier's leads ("cart's calls to inventory fail ... find out what is wrong with inventory"); every read is an MCP tool call through the gateway; each model request is marked replayed | two independent specialists with their own tools and their own evidence |
| 1:05 - 1:30 | Evidence & Decisions | round 1: two supported dependency failures (frontend on checkout, cart on inventory), not actionable by themselves; a claim that cart's recent deployment broke frontend **not accepted**: it cited none of the evidence its rules relied on; round 2: resource pressure on inventory supported by its OOMKilled and BackOff events | evidence rules decide, never a vote between agents |
| 1:30 - 2:00 | the approval dialog (Incident Lab) | `restart_service` on `inventory`, parameters `{}`, risk `reversible_write`, the proposal's digest and its expiry; press **Approve** | a person approves one exact action, once |
| 2:00 - 2:20 | Safety & Approvals | the policy said APPROVAL_REQUIRED (`R11-reversible-approval`), then ALLOW only for your approval (`R12-approval-binding`); the write went out under that approval's id and the approval is now used | the authorization chain |
| 2:20 - 2:40 | Incident Lab, the result | the oracle: unhealthy before, healthy after, inventory `CrashLoopBackOff` to `Running`, every error rate 0; state RESOLVED ("oracle verified recovery (simulated environment)"); memory: nothing written (a replay is not new experience) | recovery judged by the environment, not by the model |
| 2:40 - 2:50 | provenance | the recording id `20261025-121828-ollama-6a5cfd`, its SHA-256, the export folder; this is a replay, not live inference | a reviewer can check every claim |

If you **reject** instead, the run ends BLOCKED and nothing is written to the simulated
shop: the model's proposal never acts by itself.

## What each screen is reading

The dashboard draws only what the API returns from the run's stored record and trace; it
never makes up a number. For this run:

- **Model requests**: 14, each marked replayed, with the original run's token counts (as
  the server reported them) and durations (24 to 123 seconds each, as RECAP measured them
  in the live run). The replay itself
  calls no model and spends no tokens: its usage says 0 model requests and the cost line
  says "$0.00 - replayed recording".
- **Timing**: the overlap of the two specialists that a replay shows is the replay's own
  (a few seconds at the Watchable pace, well under a second at Instant), not the original
  run's (146.9 s in round 1). Each model request still shows the original run's own times,
  including how long it waited inside the model server: one local server answered the two
  specialists' requests one after another. The original timings are in the export's README.
- **Evidence IDs** like `ev-<run>-r1t03` (round 1, telemetry, third item) and
  `ev-<run>-r2i02` (round 2, infrastructure): each specialist numbers its own evidence, so
  the replay asks exactly what the original run asked, whichever agent is faster.
- **The approval** is a fresh one, by you, for this run's proposal. The original run's
  approval belonged to its own proposal and digest, was used once and has expired; it is
  never reused.

## Why you can trust the replay

- CI replays this recording on every push (`tests/e2e/test_golden_replay.py`): the event
  shapes, the approval chain from request to oracle, that nothing is written before the
  approval, the oracle's verdict against a fresh probe of the simulated shop, the replay
  labels, and that a copy with one changed byte is refused before any request is answered.
- The same test holds back one specialist's tool calls, then the other's: the agents
  interleave differently, yet every finding, verdict, proposal, fix and oracle result is
  the same. Requests are matched by what they ask, never by their order
  ([ADR 0018](decisions/0018-record-every-model-exchange-and-replay-in-ci.md),
  [ADR 0026](decisions/0026-a-replay-stops-where-its-recording-stopped.md)).
- A replay never acts on anything real: RECAP refuses it before it starts if the
  configured environment is not simulated
  ([ADR 0028](decisions/0028-content-addressed-recordings-and-replay-by-manifest-id.md)).

## Optional: record your screen (macOS, built in)

1. Sign in to the dashboard first, close the terminal that printed the token, and turn on
   Do Not Disturb so no notification appears.
2. Press **Shift-Command-5** and choose **Record Selected Portion** (or **Record Entire
   Screen**); under **Options** pick no microphone unless you narrate. Click **Record**.
   (QuickTime Player's **File > New Screen Recording** does the same.)
3. Follow the tour above. Keep the `REPLAY`, `RECORDED REAL-LLM RUN` and `SIMULATED
   ENVIRONMENT` labels in the frame, and say it in words: "this is a replay of a recorded
   run, not live inference".
4. Stop with the stop button in the menu bar (or **Control-Command-Esc**). Trim it in
   QuickTime Player (**Edit > Trim**).
5. Before sharing, watch it once: no token, no `operator-token` file, no terminal with a
   home folder path, no other windows. Keep the video out of the repository.

## A live run on a real cluster (M5)

The replay above runs against the simulated shop and needs nothing but this repository.
Since M5 the same dashboard can also run an incident on a real Kubernetes cluster: RECAP's
Kind demo cluster on the owner's AWS VM, with `qwen3:8b` served there. That is not a
quickstart: it needs the owner's VM, the cluster, RECAP's identities on it and the SSH
tunnel, and a run takes 15 to 30 minutes. The steps are the reproduction rows of the M5
report ([m5-report.md, section 11](m5-report.md#11-reproduction)): "The VM", "The VM's model
server" and "A Kind run from Mission Control". What differs on screen:

- the strip reads LIVE ENVIRONMENT · kind, and each run is labelled a live target, not a
  simulation;
- on **Overview**, **Check now** asks the server to reach the cluster as a run would, and
  changes nothing;
- in the **Incident Lab** a run is a new sample from the model, never a replay (a replay is
  refused against a live target), and approval is manual only;
- the approval dialog shows a red LIVE block naming the cluster and the namespace, and
  **Approve** stays disabled until you type the target service's exact name (`backend` for
  `kind-bad-release`); a rejection needs no name;
- the oracle reads the cluster with its own identity, so RESOLVED means the cluster itself
  recovered.

M5's acceptance run, `run-34a3580d2f`, went this way: started and approved in the dashboard,
RESOLVED, the recovery verified by the oracle
([its export](../evals/live/2026-11-07-qwen3-8b-kind-dashboard-approved-prompts-v4.7/README.md)).
It is one run, not a success rate.

## Limits of this demo

- One recorded run of one held-out simulated incident. It is evidence that the path works
  with a real model's decisions, not a success rate: `qwen3:8b` still fails the
  cross-service incident (see [docs/status.md](status.md)).
- The recording replays only with the prompts it was recorded with (`prompts-v4.7`). After a
  prompt change the replay stops with `ReplayMismatch` and the run must be recorded again.
- The replay touches no Kubernetes cluster or other real system: it always runs against the
  simulated shop. A run on a real cluster is the section above, on the owner's setup.
