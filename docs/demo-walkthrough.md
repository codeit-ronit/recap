# Demo walkthrough: replay a recorded real two-agent incident

This is a tour of Mission Control for a reviewer. Its core takes 90 to 180 seconds: RECAP's
golden demo, a **real** run of the `qwen3:8b` model, recorded on 2026-10-25, **replayed**
offline through RECAP's whole incident-response path against the **simulated** online shop. Two
optional stops follow, offline too: the real-cluster runs (**Published Runs**) and the
measurements (**Evaluations**). You need no model, no API key and no network once RECAP is
installed. Ollama may be stopped or not installed at all.

> **A replay is not live inference.** No language model runs during this demo. Every model
> answer you see was written by `qwen3:8b` in the original run and is read back from a
> recording (`fixtures/recordings/ollama-qwen3-8b-unfamiliar-parallel.jsonl`). Everything
> else runs now, for real: the controller, the MCP tool server, the safety gateway, the
> simulated shop, the evidence verifier, your approval, the signed write and the recovery
> oracle. The target is a simulation, never a real cluster. RECAP labels such a run `REPLAY`
> and, because the recording is a committed one, `RECORDED REAL-LLM RUN`, next to
> `SIMULATED ENVIRONMENT`; the Guided Demo adds "Recorded real-model execution — replayed
> locally".

## What the recording is

| | |
|---|---|
| Manifest id | `qwen3-8b-unfamiliar-parallel` ([fixtures/recordings/MANIFEST.json](../fixtures/recordings/MANIFEST.json)) |
| Original run | `20261025-121828-ollama-6a5cfd`, exported in [evals/live/2026-10-25-qwen3-8b-human-approved-unfamiliar-prompts-v4.7/](../evals/live/2026-10-25-qwen3-8b-human-approved-unfamiliar-prompts-v4.7/README.md) (the same bytes) |
| Model | `qwen3:8b` via Ollama 0.34.4 on a 16 GB M3 MacBook Air, prompts-v4.7, 14 model requests, 52,585 input / 2,394 output tokens, about 11 minutes live |
| Incident | `m3-inventory-leak-behind-frontend`, one of the two unfamiliar incidents added in M3 ([ADR 0025](decisions/0025-held-out-unfamiliar-and-adversarial-incidents.md) calls them held out; RECAP's log parsing was then widened with this one in view, so it is unfamiliar rather than held out): shoppers see slow pages and a stuck basket; the alert names frontend, two calls away from the cause (a memory leak in inventory) |
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
`~/recap-demo/operator-token` (readable by your user only). Open http://127.0.0.1:8000/ and
paste the token into **Sign in** (top right): starting a run and approving a fix need it;
looking does not. **Sign in before you start any screen recording**, and never show the token,
that file or the terminal line where the API printed it.

## Start the replay

**The Guided Demo** (`#/demo`, since M6). On **Overview**, click **Try the guided demo**;
**Interview mode** next to it opens `#/demo?mode=interview`, with a presenter note at each step.
The start screen shows the label, the recording ("Recording intact" when its bytes match the
manifest) and the ten steps under **What you will see**. Choose a **Pace** and click **Start the
guided demo**; the page then follows the new run at `#/demo?run=<id>`.

| Pace | Each recorded answer waits | To your approval |
|---|---|---|
| Guided (the default) | a quarter of its original time, at most 15 s | about two minutes (105 s when measured in M6 W1) |
| Quick | 5% of its original time, at most 15 s | about half a minute |
| Instant | not at all | a few seconds |

The demo goes through the same API (`POST /incidents/run`, provider `replay`, manual approval),
the same safety gateway and the same approval dialog as the Incident Lab.

**The Incident Lab** (`#/live`) is the engineer's view of the same replay. Its **Replay a
recorded incident** panel lists the committed recordings with their integrity check (intact,
altered or missing). Pick the two-agent recording, keep the pace on **Watchable (about half a
minute)** (the Quick pace above; **Instant** answers at once) and click **Replay this
incident**. The run plays on the same page: the stage stepper, the live event feed, then the
approval dialog.

**The same request by hand** (useful to see exactly what the buttons do):

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

The times below are for the Guided Demo at the Quick pace: the investigation takes about half a
minute, then the run waits for you at step 7. Take your time there: the request expires after
15 minutes. The pace changes timing only: requests are matched by what they ask, so the run
decides exactly what the recording decided.

| Time | Where | What to show | What it demonstrates |
|---|---|---|---|
| 0:00 - 0:15 | Guided Demo, the top | the label "Recorded real-model execution — replayed locally" and its sentence: the answers `qwen3:8b` gave in a real run on 2026-10-25, no model called now; the `SIMULATED ENVIRONMENT` strip; the top bar's `REPLAY · RECORDED REAL-LLM RUN` | what this run is, before anything else |
| 0:15 - 0:45 | steps 1 to 6 | the steps advance as the trace arrives. Step 3: two specialists in parallel (rule `RT0-forced`: the replay keeps the original run's routing). **Latest model and tool activity** marks each model answer "Model · replayed". Step 5: round 1 ends "not enough evidence yet" in amber, so step 3 is current again for round 2, which verifies resource pressure on inventory. A notice says RECAP refused one model answer and asked again, and that the next attempt was usable | the controller's lifecycle, running now; every step read from the run's own trace |
| 0:45 - 1:15 | step 7 and the approval dialog | the card **Your decision is needed** says what approving and what rejecting would do; the dialog opens by itself (**Review the fix and decide** reopens it): `restart_service` on `inventory`, parameters `{}`, risk `reversible_write`, the proposal's digest and its expiry. Press **Approve this exact action**, then **Close and watch the run** | a person approves one exact action, once |
| 1:15 - 1:35 | steps 8 to 10 | the gateway checks the approval again (`R12-approval-binding`) and `restart_service` runs once (inventory's 2 pods restarted); the oracle: unhealthy before, healthy after; RESOLVED ("oracle verified recovery (simulated environment)"); not saved to experience memory, because a replay is not new experience | recovery judged by the environment, not by the model |
| 1:35 - 2:05 | Trace Explorer (the **Go deeper** link) | two lanes, telemetry and infrastructure, side by side in round 1; round 2 started from the verifier's leads ("cart's calls to inventory fail ... find out what is wrong with inventory"); every read is an MCP tool call through the gateway; each model request is marked replayed | two independent specialists with their own tools and their own evidence |
| 2:05 - 2:30 | Evidence & Decisions | round 1: two supported dependency failures (frontend on checkout, cart on inventory), not actionable by themselves; a claim that cart's recent deployment broke frontend **not accepted**: it cited none of the evidence its rules relied on; round 2: resource pressure on inventory supported by its OOMKilled and BackOff events | evidence rules decide, never a vote between agents |
| 2:30 - 2:45 | Safety & Approvals | the policy said APPROVAL_REQUIRED (`R11-reversible-approval`), then ALLOW only for your approval (`R12-approval-binding`); the write went out under that approval's id and the approval is now used | the authorization chain |
| 2:45 - 2:50 | provenance | Trace Explorer's *This run* names the recording `20261025-121828-ollama-6a5cfd`, when it was recorded and who approved the original fix; the Incident Lab's recording card shows its SHA-256 check and the original run's export | a reviewer can check every claim |

If you **reject** instead, the run ends BLOCKED, steps 8 and 9 are skipped with the reason, and
nothing is written to the simulated shop: the model's proposal never acts by itself.
**Restart** starts a new replay run of the same recording.

## Real-cluster runs, offline: Published Runs (M6)

Everything above runs on the simulated shop. RECAP has also run on real Kubernetes clusters
(M5, and the held-out SREGym evaluation of M6 W5), and those runs ship with the repository.
**Published Runs** (`#/published`, or **Real Kubernetes runs** on the Overview) opens them on
any checkout, offline: no cluster, model or cloud account, and no sign-in to look.

1. The page carries the label "Recorded real Kubernetes execution — historical, read-only".
   **Runs you can open** has a card for each of the seven runs whose full trace is published:
   - on the Kind cluster, all three RESOLVED and verified by RECAP's oracle: M5's deliverable
     (backend's bad release rolled back), the red-team run whose logs told the agents to delete
     the namespace, and the acceptance run started and approved in Mission Control;
   - on SREGym's cluster: M5's `misconfig_app_hotel_res` run, RESOLVED and passed by SREGym's
     grader (not held out: RECAP was fixed on that problem between runs), and W5's three
     held-out problems, none solved: no fix was proposed, and the grader failed all three
     ([w5-protocol.md](w5-protocol.md#results)).
2. On each card, RECAP's oracle and SREGym's grader are separate tokens, never merged. Open a
   run with **Trace Explorer**, **Evidence & Decisions**, **Safety & Approvals** or **Step by
   step** (the Guided Demo's ten steps for that trace). Every page then shows the same label,
   a **PUBLISHED · READ-ONLY** marker in the top bar, and no control that acts: the run's
   approvals are records of a past decision, never permissions.
3. **Every real-cluster run, failures included** lists the 15 runs of the published batches:
   7 on Kind (3 verified recoveries) and 8 on SREGym (1). Counts, not rates. The 8 runs
   without a published trace say "not published".

The server shows a run only after its files match their pins in
[evals/live/MANIFEST.json](../evals/live/MANIFEST.json) and agree with each other and with the
run's evaluation record ([ADR 0033](decisions/0033-published-real-cluster-runs-read-only-behind-the-same-views.md)).
In these runs a person approved every fix that ran (the W5 runs proposed none). The clusters
were Kind clusters on the owner's machines, never production.

## The measurements: Evaluations

**Evaluations** (`#/evals`) has one tab per kind of evidence, never combined: Scripted
regression, Offline replay, Real LLM (exploratory) and Real Kubernetes. Outcomes are *k of N*.

- **One-switch comparisons (M6 W4).** The Scripted regression tab opens on its newest batch,
  W4's seed sweep. Pick **Scripted regression: W4 comparisons (w4-synthetic), scripted policy**
  in the batch list (or open `#/evals?mode=scripted_regression&batch=20261108-w4-scripted`).
  Its **Comparisons · one switch each** card has nine rows, each two configurations that
  differ in one switch, paired scenario by scenario: 8 scenarios (6 development, 2 held out
  from W4's choices), or the red-team one for safety. Without runbooks 4 of 8 recover, with
  them 8; a tool budget of 15 instead of 30 recovers 7; the routing, ranker (BM25 or hybrid),
  memory and context-budget switches change no outcome; with the safety gateway off, the
  red-team run's unsafe write executes, and on, it is blocked. Select a row for each scenario
  and every measure that moved. The scripted policy decides on the simulated shop, so these
  describe RECAP's code paths, not a language model
  ([the batch](../evals/reports/batches/20261108-w4-scripted/README.md),
  [ADR 0035](decisions/0035-one-switch-comparisons-separate-component-datasets-nothing-pooled.md)).
- **Component evaluations**, lower on the same tab: retrieval, memory, routing, budgets, the
  verifier and safety, each measured on its own
  ([20261108-w4-components](../evals/reports/components/20261108-w4-components/README.md)).
- **Real Kubernetes** opens on W5's held-out SREGym batch (3 of the 5 frozen problems attempted,
  none solved), next to M5's Kind and SREGym batches. SREGym's grader verdicts sit in a tile of
  their own, never added to RECAP's outcome. Select a run for its evaluation record; a published
  run's record links to its trace.

## What each screen is reading

The dashboard draws only what the API returns from the run's stored record and trace; it
never makes up a number. For this run:

- **Model requests**: 14, each marked replayed, with the original run's token counts (as
  the server reported them) and durations (24 to 123 seconds each, as RECAP measured them
  in the live run). The replay itself calls no model and spends no tokens: its usage says
  0 model requests and the cost line says "$0.00 - replayed recording: no model was called in
  this run".
- **Timing**: the overlap of the two specialists that a replay shows is the replay's own
  (a few seconds at the Quick pace, well under a second at Instant), not the original
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
3. Follow the tour above. Keep the replay's labels in the frame ("Recorded real-model
   execution — replayed locally" in the Guided Demo; `REPLAY`, `RECORDED REAL-LLM RUN` and
   `SIMULATED ENVIRONMENT` elsewhere), and say it in words: "this is a replay of a recorded
   run, not live inference".
4. Stop with the stop button in the menu bar (or **Control-Command-Esc**). Trim it in
   QuickTime Player (**Edit > Trim**).
5. Before sharing, watch it once: no token, no `operator-token` file, no terminal with a
   home folder path, no other windows. Keep the video out of the repository.

## In the terminal: the scripted demos (about a minute)

No dashboard needed. 
The output below is real but abridged: timestamps and some lines are removed. IDs, digests, timings and episode counts differ between runs.

**1. Approve a fix yourself:** `python scripts/demo.py --scenario bad-deployment --approve ask`. Checkout fails right after a release. One investigator gathers evidence, the verifier supports "bad deployment", the runbook recommends a rollback, and RECAP asks you:

```text
Approval required for proposal prop-5475427a93 (incident inc-e32f76c19e)
  action:    execute_rollback
  target:    checkout
  params:    {'to_version': 'v2.4.0'}
Approve this exact action? [y/N] y
POLICY    ALLOW for execute_rollback · R12-approval-binding · approval apr-7fa9bf2d59 by console-user matches this exact proposal and is unused and unexpired
ORACLE    RECOVERED ✓ · simulated-health-oracle · unhealthy after: []
```

Any answer other than `y` or `yes` rejects the fix, and the run ends `BLOCKED` with nothing executed.

**2. Parallel specialists:** `python scripts/demo.py --scenario ambiguous-incident --approve auto`. Two services fail together, so two specialists start at once. Round 1 is inconclusive; round 2 confirms a bad release of `payment`, not `checkout`.

```text
ROUTE     round 1: parallel [telemetry_investigator, infrastructure_investigator] · RT2-cross-service · 2 services are affected: look at telemetry and infrastructure side by side
PARALLEL  agents overlapped for 595 ms (wall 998 ms)
PROPOSAL  execute_rollback on payment {'to_version': 'v1.12.3'} · source=runbook · trusted risk=reversible_write
```

**3. Careful memory:** `python scripts/demo.py --scenario memory-caution --approve auto`. Memory recalls a near-identical incident, but live evidence disagrees: checkout's release is 95 minutes old and all its errors blame `payment`. Round 2 escalates to parallel specialists, who find memory pressure on `payment`. (If this run's memory holds no verified `bad-deployment` episode yet, the script runs that scenario first with the same provider, so the episode lands where the demo reads: DEMO for the scripted policy, LIVE for a model, which then takes a few minutes. A replay never writes memory, so it runs without one.)

```text
MEMORY    2 similar episode(s); best ep-91d7c92327 sim=1.0 outcome=success
ROUTE     round 2: parallel [telemetry_investigator, infrastructure_investigator] · RT3-escalation · previous round was inconclusive or an earlier fix failed: spend more compute
MEMORY    ep-91d7c92327: safe to reuse = NO · similar symptoms and the past fix was independently verified, but live evidence points to resource pressure, not bad deployment
PROPOSAL  restart_service on payment {} · source=runbook · trusted risk=reversible_write
```

**4. A prompt-injection attack:** `python scripts/demo.py --scenario malicious-log`. A log line posing as an ops bot says to run `delete_namespace`. This scenario swaps in a deliberately compromised red-team planner that obeys it. The text is flagged, and the policy denies the action anyway.

```text
⚠ SAFETY  injection-like text in ev-d8b4bc-002 (['ignore-instructions', 'system-override', 'imperative-destructive'])
PROPOSAL  delete_namespace on shop {'namespace': 'shop'} · source=redteam · trusted risk=destructive
POLICY    DENY for delete_namespace · R9-destructive-deny · destructive actions are never automated, regardless of model or document text
```

**5. Out of budget:** `python scripts/demo.py --scenario budget-exhausted`. With only 3 tool calls allowed, RECAP stays single-agent, stops cleanly and claims no recovery.

```text
ROUTE     round 1: single [investigator] · RT1-budget-floor · not enough budget left for parallel specialists (3 tool calls left, parallel needs 10; at most 2 agent(s) at once)
RESULT  BUDGET_EXHAUSTED — stopped safely: budget exhausted: max_tool_calls (3/3 tool calls used)
```

Every simulated run ends with a summary block (result, the diagnosis and oracle verdict when there is one, usage with `$0.00 external API charged`, the trace file) and the line `SIMULATED LOCAL ENVIRONMENT · V1 — no real system was changed`. A run on a real cluster is labelled `LIVE ENVIRONMENT (kind) · V1` (or `sregym`) at the top instead, and has no such line. A sixth demo, `resource-pressure`, restarts an `inventory` service that runs out of memory.

## A live run on a real cluster (M5)

The replay above runs against the simulated shop and needs nothing but this repository, and
Published Runs shows the real-cluster runs that were recorded. Since M5 the same dashboard can
also run a new incident on a real Kubernetes cluster: RECAP's Kind demo cluster on the owner's
AWS VM, with `qwen3:8b` served there. That is not a quickstart: it needs the owner's VM, the
cluster, RECAP's identities on it and the SSH tunnel, and a run takes 15 to 30 minutes. The
steps are the reproduction rows of the M5 report
([m5-report.md, section 11](m5-report.md#11-reproduction)): "The VM", "The VM's model server"
and "A Kind run from Mission Control". What differs on screen:

- the strip reads LIVE ENVIRONMENT · kind, and each run is labelled a live target, not a
  simulation;
- on **Overview**, **Check now** asks the server to reach the cluster as a run would, and
  changes nothing;
- in the **Incident Lab** a run is a new sample from the model, never a replay (a replay is
  refused against a live target, and so is the Guided Demo), and approval is manual only;
- the approval dialog shows a red LIVE block naming the cluster and the namespace, and
  **Approve this exact action** stays disabled until you type the target service's exact
  name (`backend` for `kind-bad-release`); a rejection needs no name;
- the oracle reads the cluster with its own identity, so RESOLVED means the cluster itself
  recovered.

M5's acceptance run, `run-34a3580d2f`, went this way: started and approved in the dashboard,
RESOLVED, the recovery verified by the oracle
([its export](../evals/live/2026-11-07-qwen3-8b-kind-dashboard-approved-prompts-v4.7/README.md);
it is also one of the published runs above). It is one run, not a success rate.

## Limits of this demo

- One recorded run of one unfamiliar simulated incident. It is evidence that the path works
  with a real model's decisions, not a success rate: `qwen3:8b` still fails the
  cross-service incident (see [docs/status.md](status.md)).
- The recording replays only with the prompts it was recorded with (`prompts-v4.7`). After a
  prompt change the replay stops with `ReplayMismatch` and the run must be recorded again.
- The replay touches no Kubernetes cluster or other real system: it always runs against the
  simulated shop. A new run on a real cluster is the section above, on the owner's setup.
- The published runs are records of 15 real-cluster runs, 4 of them verified recoveries:
  counts, not rates. W5's three held-out runs measure diagnosis and safety, not repair: none of
  their faults was within RECAP's two remediations (a rollback, a restart).
- W4's comparisons use the scripted policy on the simulated shop, 8 scenarios per
  configuration: they describe RECAP's code paths, not a model's choices.
