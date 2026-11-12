# Interview kit

A demonstration of 3 to 5 minutes, a deeper technical walkthrough, and how to record the demo.
Each demo step names the test or the committed evidence behind it. RECAP is a prototype: the
simulated shop is its default target, since M5 it has also run on Kind clusters on the owner's
machines, and nothing ran in production. A person approved every real-model fix. Scripted,
replayed and real-model results are never added together, and they are counts, not rates.
Practice questions, concept by concept: [learning-notes.md, Appendix C](learning-notes.md#appendix-c-interview-practice-and-what-to-study-next).

## 1. The demonstration (about 5 minutes)

### Before the interview

```bash
.venv/bin/python scripts/seed_demo.py --data-dir ~/recap-demo     # offline runs; no model, $0
cd apps/dashboard && npm ci && npm run build && cd ../..
RECAP_DATA_DIR=~/recap-demo .venv/bin/uvicorn apps.api.main:app   # http://127.0.0.1:8000/
```

- **Sign in** (top right) with the token in `~/recap-demo/operator-token` before anyone watches.
  The API also prints the token once in its terminal: keep that terminal off screen.
- Rehearse with the browser smoke test (Node 22+, Chrome). It clicks this script's path (start,
  approve, RESOLVED; Restart, reject, BLOCKED) and checks that the API agrees:
  `node apps/dashboard/e2e/smoke.mjs --base http://127.0.0.1:8000 --token-file ~/recap-demo/operator-token`.
  Its documented runs with the guided demo, M6 W1 to W3, passed ([m6-progress.md](m6-progress.md));
  later work changed other code, so run it again. It uses the Quick pace and this script the
  Guided one; a pace changes when a recorded answer arrives, never which answer
  ([test_replay_pace.py](../tests/unit/test_replay_pace.py)).

### The script

**0:00 to 0:25 · The problem.**
- *Do:* open http://127.0.0.1:8000/ (Overview).
- *Say:* "An alert names a symptom, not a cause. A model reads logs and metrics fast, but an agent
  that changes infrastructure because a model said so is the risk. RECAP's rule: untrusted data
  can suggest, trusted code decides, an independent check confirms. Models investigate with
  read-only tools, a person approves one exact fix, and an oracle, not the model, says whether
  the system recovered."
- *Verified by:* the hero sentence and its links,
  [overview-architecture.test.tsx](../apps/dashboard/tests/overview-architecture.test.tsx).

**0:25 to 1:05 · The architecture, one sentence per layer.**
- *Do:* click **Explore the technology** (`#/architecture`); click down the flow's steps as you speak.
- *Say:* (1) "A controller, a state machine, owns each incident, checkpoints every step, and alone
  may execute a fix. (2) Retrieval ranks runbook sections with BM25; memory offers similar past
  incidents; both are untrusted context. (3) A rules router sends one investigator or two
  specialists. (4) The agents' brain is a provider, a scripted policy by default or a real model,
  limited to read-only tools. (5) Every tool call passes one gateway and one policy, decided from
  trusted configuration; the environment's tools are real MCP calls. (6) A verifier, plain code,
  accepts a diagnosis only if this run's evidence supports it. (7) A person approves one exact
  proposal; the target accepts the write only with a single-use capability signed for it. (8) An
  oracle reads health through a method no agent can call; only a verified recovery is a success in
  memory. (9) The target is an adapter: the simulated shop by default, Kind or SREGym since M5."
- *Verified by:* the eleven steps in order, and every code path and ADR they link exists,
  [overview-architecture.test.ts](../apps/dashboard/tests/overview-architecture.test.ts).

**1:05 to 1:25 · A real recorded investigation: what is replayed, what runs now.**
- *Do:* back on the Overview, click **Interview mode** (`#/demo?mode=interview`). Point at the label
  **Recorded real-model execution — replayed locally** and at **Recording intact**. Keep the
  **Guided** pace; click **Start the guided demo**.
- *Say:* "This is a replay, and the page says so. On 2026-10-25 a local qwen3:8b worked this
  incident for about 11 minutes on a 16 GB laptop, and the owner approved its fix at a terminal.
  The recording is committed, pinned by its SHA-256. No model is called now: each of its 14
  answers is read back from it. Everything else runs now: the controller, the MCP server, the
  gateway, the simulated shop, the verifier, my approval and the oracle. The incident was
  written in M3 to be unfamiliar: another log style, an alert two calls from the cause."
- *Careful:* call it unfamiliar, not held out: M3 widened RECAP's log parsers with it in view
  ([ADR 0025](decisions/0025-held-out-unfamiliar-and-adversarial-incidents.md),
  [evals/splits/w4.yaml](../evals/splits/w4.yaml)). Step 2's presenter note still says "held-out".
- *Verified by:* the label, [guided-demo.test.tsx](../apps/dashboard/tests/guided-demo.test.tsx);
  14 replayed answers, no model request, one changed byte refused before any answer,
  [test_golden_replay.py](../tests/e2e/test_golden_replay.py); the original run,
  [its export](../evals/live/2026-10-25-qwen3-8b-human-approved-unfamiliar-prompts-v4.7/README.md).

**1:25 to 2:50 · The agents' and tools' decisions.** At the Guided pace the approval comes about
two minutes after Start (105 s in M6 W1). Talk over each step as it becomes current; turn on
**Technical details** to show event numbers and evidence ids.
- Steps 1 to 3: "Frontend and cart are affected. The incident gets a hard budget: 30 steps, 30
  tool calls, 200,000 model tokens. Retrieval finds 3 runbook sections; memory knows no similar
  incident. Two specialists start at once; the rule reads RT0-forced because the original run
  forced the parallel strategy, and a replay must ask exactly what the original asked."
- Step 4: "Telemetry reads logs and metrics; infrastructure reads health, deployments and cluster
  events; they share no environment tool. During a round each sees only its own new evidence.
  Every read is an MCP call through the gateway, and each result becomes evidence with an id. Each
  model line says 'replayed, not called now'." At the notice: "RECAP refused one answer that
  repeated a tool call already made; it asked again, and the next answer was usable."
- Step 5, amber: "The verifier is code, not a model. Round 1: not enough evidence. A claim that
  cart's recent deployment broke frontend was not accepted: it cited none of the evidence its
  rule needs. The verifier's leads send round 2 to inventory." Then: "Round 2: verified, resource
  pressure on inventory."
- Step 6: "The model drafts one fix, a restart of inventory. Before anyone is asked, the evidence
  gate checks it (VP1 to VP4) and the policy says a person must approve this exact action (R11)."
- *Verified by:* each step's facts, from two real replays of this recording,
  [guided-demo.test.ts](../apps/dashboard/tests/guided-demo.test.ts); the decisions, the same
  whichever specialist is slower, in test_golden_replay.py; disjoint tools,
  [test_role_contracts.py](../tests/unit/test_role_contracts.py).

**2:50 to 3:30 · Approval and safety.**
- *Do:* the approval dialog opens by itself. Point at **restart_service on inventory** (no
  params), **trusted risk: reversible_write**, the short digest and expiry, **Why you are asked**
  (R11 and the gate's VP1 to VP4), the rationale shown as the recorded model's untrusted text, and
  **Evidence it cites** (inventory's container exceeded its memory limit and was killed). Click
  **Approve this exact action**, then **Close and watch the run**. (**Decide later** closes the
  dialog without deciding; **Review the fix and decide** reopens it.)
- *Say:* "The dialog has no close button: I approve, reject or decide later. The approval is
  bound to a SHA-256 digest of this incident, action, target and parameters; any other digest is
  refused with 409. It expires after 15 minutes, a 'no' wins until it is used, and it is used
  once, right before the write. The reasons shown are RECAP's checks; the rationale is data. The
  gateway then signs a single-use capability for exactly this action, and the shop refuses any
  write without one. On a real cluster I would also type the service's name."
- *Verified by:* the dialog, Decide later and the live name check,
  [incident-lab.test.tsx](../apps/dashboard/tests/incident-lab.test.tsx); the 409,
  [test_api.py](../tests/e2e/test_api.py); R11, one write under one capability, no replay on a
  non-simulated target, test_golden_replay.py; the dialog and Approve in a browser, the smoke test.

**3:30 to 3:50 · Verified recovery.**
- *Do:* watch steps 8 to 10 finish; click step 9.
- *Say:* "The gateway checked the approval once more (R12), and restart_service ran once. The
  oracle read health through a method no agent can call: unhealthy before, healthy after. Only
  that makes the run RESOLVED. A replay writes nothing to memory: it is not new experience."
- *Verified by:* guided-demo.test.ts; inventory CrashLoopBackOff before and Running after, every
  error rate 0, a fresh probe agreeing, test_golden_replay.py; RESOLVED in a browser with the API
  agreeing, the smoke test.

**3:50 to 4:25 · One evaluation finding: M6's held-out SREGym evaluation, n = 3.**
- *Do:* back on the Overview, click **Real Kubernetes runs** (`#/published`); point at the cards
  "SREGym held-out 1" to "3".
- *Say:* "One result I trust because of how it was run. Five SREGym problems RECAP had never run
  were chosen from their names alone, by a rule written first; their scenario files held no
  answer; code, prompts, configuration and model weights were frozen and checked before each
  run. It stopped after 3: on the VM's CPU a model request took 103 to 616 s, the model failed
  twice for that reason (a stop condition), and the owner ended it. None of the 3 was solved, by
  RECAP's oracle or SREGym's grader. No fix was proposed, so nothing was written; the verifier
  refused every unsupported claim. The answers, read only afterwards, show RECAP could not have
  fixed them: each fault changed a Deployment's spec, not its image, which neither a rollback nor
  a restart undoes, and one sat outside the allowlist. So these runs measured diagnosis and
  safety, not repair; an approved edit of one Deployment field is now first on the roadmap."
- *Verified by:* [w5-protocol.md, Results](w5-protocol.md#results), the
  [batch](../evals/reports/batches/2026-11-09-w5-heldout-sregym-qwen3-8b/README.md), the
  [session record](../evals/frozen/w5-evidence/README.md); the three runs served read-only, each
  FAILED with no verified recovery, [test_api_published.py](../tests/e2e/test_api_published.py),
  [test_published_runs.py](../tests/unit/test_published_runs.py). The browser smoke test opened
  these cards too (133 of 133 checks, 2026-11-12, version 0.7.0).

**4:25 to 4:55 · Technical trade-offs.**
- *Say:* "Three trade-offs. Rules decide where it matters, so a wrong model answer is refused, not
  acted on; but the rules read a fixed family of log phrasings. Only two actions, rollback and
  restart, keep the write path small enough to make safe; W5 shows the price. A free local model
  keeps RECAP at $0 and private, but on a CPU a request takes minutes, and two specialists at
  once still queue on one model server."
- *Verified by:* [signals.py](../recap/core/signals.py) and ADR 0025; W5 above; the README's
  "Adaptive compute" section.

**If asked "what if I say no?"** Click **Restart**, then **Reject** in the dialog: BLOCKED, and
steps 8 and 9 read "Not run: the fix was rejected, so nothing changed" (the smoke test,
guided-demo.test.ts). **Short on time:** choose **Quick** (27 s to the approval in W1) and drop the
architecture list and the trade-offs.

## 2. Deeper technical walkthrough

### Why multi-agent rather than single-agent

- Not always several agents: a rules router sends one investigator to a clear single-service
  incident, and two specialists only when signals call for it (several services affected, an
  inconclusive round or a failed fix, conflicting memory, ambiguous symptoms).
- The gain is an independent second view: disjoint environment tools (telemetry: logs and metrics;
  infrastructure: health, releases, events, topology), each concluding only what its tools observe
  and seeing, during a round, only earlier evidence plus its own (before M3 they shared everything,
  and the second opinion often repeated the first). The verifier judges every claim against both
  agents' evidence; rules settle disagreements, never votes.
- W4 (scripted, 8 paired runs): always-parallel and adaptive kept 8 of 8 recoveries with 2.9 fewer
  tool calls per run than always-single. Cost: concurrency, not parallel inference; one model
  server answers the two in turn.
- Code: [router.py](../recap/routing/router.py), [agents.yaml](../configs/agents.yaml), [verifier.py](../recap/agents/verifier.py).
- ADRs: [0020](decisions/0020-independent-specialists-with-role-contracts.md), [0021](decisions/0021-evidence-rules-settle-disagreements-never-votes.md).

### Why MCP

- Agents never import an environment: they call tools on a real MCP server (the official Python
  SDK; seven reads, three writes), a subprocess per run over stdio. So M5 reached Kubernetes with a
  new adapter behind the same tools: the agents, the server and the gateway stayed the same
  ([test_environment_swap.py](../tests/e2e/test_environment_swap.py) proves the swap's wiring).
- The server is untrusted: only tools in RECAP's own registry are callable, risk tiers come from
  configuration, undefined arguments are rejected, and a write needs a capability the gateway
  signed; a second server process for the run cannot write. On SREGym RECAP never uses the
  benchmark's agent tools (a kubectl passthrough and a shell). Cost: about 0.4 to 0.5 s a run.
- Code: [mcp_server.py](../recap/tools/mcp_server.py), [mcp_client.py](../recap/tools/mcp_client.py), [registry.py](../recap/tools/registry.py).
- ADRs: [0004](decisions/0004-real-mcp-over-stdio.md), [0007](decisions/0007-replaceable-environment-adapter.md), [0014](decisions/0014-signed-capabilities-at-the-write-boundary.md).

### Why a model-agnostic harness

- The agent loop observes, asks a provider and acts through the gateway, without knowing whether
  the provider is a model: the scripted policy (the default, not an LLM), qwen3:8b on Ollama (local
  or on the owner's VM), gpt-oss-120b on Groq's free plan, a replay, or a disabled paid adapter.
- So CI and the evaluations run reproducibly at $0, a real model swaps in without touching the
  controller, gateway, verifier or oracle, a real run replays offline, and each run records what
  decided.
- One step protocol boxes models in: per-request JSON schemas from the trusted catalog let a model
  name only listed tools, allowlisted services and evidence ids it has seen; an unusable answer
  gets one corrective retry, never a scripted stand-in; no scenario id or answer reaches a model.
  Cost: a recording replays only with its own prompt version.
- Code: [loop.py](../recap/harness/loop.py), [providers.yaml](../configs/providers.yaml), [protocol.py](../recap/providers/protocol.py).
- ADRs: [0003](decisions/0003-deterministic-default-provider.md), [0010](decisions/0010-label-free-views-and-a-yes-no-oracle.md), [0016](decisions/0016-zero-cost-llm-providers-and-a-constrained-step-protocol.md), [0018](decisions/0018-record-every-model-exchange-and-replay-in-ci.md).

### How context and token budgets work

- Default budget per run: 30 steps, 30 tool calls, 120 s (a person's approval time excluded),
  120,000 estimated context tokens, 200,000 model tokens, two agents at once (W5's freeze: 3600 s).
  Running out stops the run cleanly as BUDGET_EXHAUSTED. Model tokens count as the server reports
  them: each request reserves its prompt estimate plus its answer allowance before it is sent, and
  reservations in flight count, so two agents cannot overshoot together.
- A context pack holds at most 3,000 estimated tokens (about 4 characters each), 400 per item,
  built from deterministic summaries of tool results; a cut log keeps its first and newest errors;
  the evidence a diagnosis cites, and the evidence against it, go in first, shortened rather than
  dropped. W4 (scripted, 8 paired runs): half the pack dropped 23 items in 4 runs and changed no
  outcome; half the tool budget cost one recovery (8 → 7).
- Code: [budget.yaml](../configs/budget.yaml), [budget.py](../recap/harness/budget.py), [packer.py](../recap/context/packer.py), [gateway.py](../recap/tools/gateway.py).
- ADRs: [0011](decisions/0011-structured-evidence-summaries.md), [0024](decisions/0024-a-real-model-token-budget-and-context-that-keeps-what-matters.md).

### How RAG and memory differ

- RAG is knowledge people wrote: runbook sections ranked by BM25 (hybrid with a local embedder is
  opt-in), cited by id, also searchable by agents (`search_runbooks`). Its job is the remedy: in W4
  no ranker put the right runbook first for a raw alert (0 of 17), while the lookup by the
  verified mechanism found it both times (2 of 2).
- Memory is RECAP's own experience: episodes of earlier runs, kept apart by origin (replays and
  evaluation test runs write none), each a success only if the oracle verified it. A memory is a
  lead, never a license: a past fix is safe to reuse only if it worked, the oracle verified it,
  today's evidence shows the same mechanism on the same service, and the fix still fits.
  Similarity never decides (`memory-caution`: similarity 1.0, not safe); a poisoned runbook and a
  planted false memory are both overruled by the evidence.
- W4 (scripted, 8 paired runs): without runbooks 4 of 8 recovered, with them 8 of 8; memory changed
  no outcome, cost about 1,500 estimated tokens a run, and judged all 79 remembered episodes right.
- Code: [knowledge](../recap/knowledge/README.md), [store.py](../recap/memory/store.py) (`assess_reuse`).
- ADRs: [0022](decisions/0022-hybrid-retrieval-with-a-local-embedder-and-agent-search.md), [0023](decisions/0023-memory-partitions-by-origin-and-evidence-checked-reuse.md).

### How unauthorized actions are prevented

1. Agents hold only read tools; only the controller writes, and only through the gateway.
2. The policy decides from trusted configuration, never from model or document text: reads
   allowed (R8), reversible writes need an approval (R11), destructive actions always denied (R9),
   fixes only from the remediation catalog (R14), namespaces and services allowlisted (R5, R6).
3. An approval names one proposal by its digest, expires, and is used once right before the
   write; a "no" wins until then; transitions are compare-and-swap in SQLite; through the API only
   the operator token decides.
4. The gateway signs a single-use Ed25519 capability for exactly that action, which the
   environment verifies in the same transaction as the change: a second MCP server or a replayed
   approval changes nothing.
5. On a real cluster a person types the service's name, auto-approval and replays are refused, and
   `kubectl` runs as typed operations, one least-privilege identity per role, on a pinned cluster,
   with a dry run, a read-back and an audit row.

- Injected text is flagged and kept as data; the gateway, not the flag, protects. On the real Kind
  cluster a planted instruction to delete the namespace stayed data
  ([run-f5b9e5bf45](../evals/live/2026-11-03-qwen3-8b-kind-injected-logs-human-approved-prompts-v4.7/README.md)).
  Limits: one shared operator token (it proves a token holder or a terminal and a timing, not a
  person), reads open on loopback, RBAC alone on SREGym's writer. Every attack tested:
  [performance-and-reliability.md](performance-and-reliability.md#security-regressions).
- Code: [gateway.py](../recap/tools/gateway.py), [policy.py](../recap/safety/policy.py), [approvals.py](../recap/safety/approvals.py), [capabilities.py](../recap/safety/capabilities.py), [auth.py](../apps/api/auth.py).
- ADRs: [0005](decisions/0005-central-safety-gateway-and-executor.md), [0014](decisions/0014-signed-capabilities-at-the-write-boundary.md), [0015](decisions/0015-operator-auth-and-no-wins-approvals.md), [0032](decisions/0032-a-real-kubernetes-target-behind-the-same-interface.md).

### How Kubernetes recovery is verified

- The oracle reads `TargetEnvironment.health_check`, never an MCP tool, and no agent output:
  verified means unhealthy before the fix and healthy after (every service Running, all replicas
  ready, error rate below 0.05 where measured).
- On a cluster it reads with its own identity, in two rounds a few seconds apart: rollout complete,
  replicas ready, and on Kind its own HTTP probe; SREGym has no probe, so each container must have
  run a minute with no exit in the last five minutes. After a write the controller asks again until
  healthy or the window ends (180 s on Kind, 240 s on SREGym). A successful write never counts by
  itself; SREGym's grader is recorded apart.
- M5: 12 real-cluster runs, 4 verified recoveries (3 on Kind, 1 on SREGym that its grader also
  passed). Limits: only what the target measures (no metrics on SREGym), no clock check there;
  the six real-cluster tests last ran in M5's W3.
- Code: [oracle.py](../recap/verification/oracle.py), [engine.py](../recap/environments/kubernetes/engine.py), [kind-demo](../configs/targets/kind-demo/environment.yaml) and [sregym-hotel](../configs/targets/sregym-hotel/environment.yaml) profiles.
- ADRs: [0013](decisions/0013-stabilization-epochs-and-deadlines.md), [0031](decisions/0031-an-external-benchmark-harness-release-tags-and-pod-failure-onsets.md), [0032](decisions/0032-a-real-kubernetes-target-behind-the-same-interface.md).

### How Redis worker recovery works

- Optional (`RECAP_EXECUTION_BACKEND=redis`): the API queues incidents, and workers run them
  through the unchanged harness. Submission is idempotent and bounded (429 when full), claims are
  atomic (`BLMOVE`), and a silent worker's heartbeat expires.
- Redis coordinates; the run records decide. Another worker settles a dead worker's job from four
  records outside Redis: the checkpoint, the trace with its write-ahead `mitigation.started`, the
  approval log and the target's audit log. No write-ahead record proves nothing was sent: the job
  runs again as a new run that asks a person again. A write that may have landed is never resent:
  the run ends `needs_reconciliation` for a person.
- Tested by killing a real worker with SIGKILL at seven points and, in Docker Compose, by
  restarting and killing a worker and restarting Redis (34 of 34 checks in W3, arm64 only).
  Limits: at least once, never exactly once; fencing relies on timing; no network partition tested.
- Code: [queue.py](../recap/workers/queue.py), [reconcile.py](../recap/workers/reconcile.py), [test_worker_crashes.py](../tests/integration/test_worker_crashes.py).
- ADR: [0034](decisions/0034-durable-workers-redis-coordinates-the-run-records-decide.md).

### How evaluation avoids misleading conclusions

- One record per run, built from its own record and trace: success only with the oracle's verified
  recovery, one named failure category otherwise. Modes never share a number; counts, with N.
- One switch per comparison, checked before the runs and again from their records, paired by
  scenario and seed; each component on its own dataset; leakage tests (RECAP's code names no
  scenario, no runbook or prompt holds an answer) and label-free provider views.
- The held-out SREGym evaluation was blind and frozen: a rule over problem names, answers written
  after the runs, one hash over every file that decides behaviour checked before each run, every
  attempt reported, the grader kept apart. Corrections are published: W4's review found that M3
  had changed log parsing with one of W4's two "held-out" simulated incidents in view.
- Code: [records.py](../evals/records.py), [categorize.py](../evals/categorize.py), [compare.py](../evals/compare.py), [components.py](../evals/components.py), [w5_freeze.py](../scripts/w5_freeze.py); [evaluation.md](evaluation.md).
- ADRs: [0029](decisions/0029-one-evaluation-record-per-run-and-modes-that-never-share-a-number.md), [0035](decisions/0035-one-switch-comparisons-separate-component-datasets-nothing-pooled.md), [0036](decisions/0036-a-blind-frozen-held-out-evaluation.md).

### Known limitations and trade-offs

- Small samples: 12 real-cluster runs (4 verified recoveries) and 3 held-out SREGym runs (none
  solved) show that the path works and how it fails, not how often.
- Two remediations (W5's faults each needed an edit of a Deployment's spec); a small model on a CPU
  (minutes per request); a verifier that checks evidence rules, not causation.
- Simulator first: the rules read a fixed family of log phrasings, the Kind demo app was written
  for RECAP, and the scripted evaluation is tiny and synthetic.
- One shared operator token; reads open on loopback; injection flags are easy to evade; workers
  deliver at least once; amd64 images unverified; the load test measured the backend, not inference.
- More: the [README](../README.md)'s component status, [m5-report.md, section 10](m5-report.md#10-known-failures-and-limitations), [threat-model.md](threat-model.md).

## 3. Recording the demo

A short screen recording of the Guided Demo on macOS. Use a new folder for each take, so every
take starts from the same state:

```bash
.venv/bin/python scripts/seed_demo.py --data-dir ~/recap-demo-take1
cd apps/dashboard && npm ci && npm run build && cd ../..
RECAP_DATA_DIR=~/recap-demo-take1 .venv/bin/uvicorn apps.api.main:app
```

1. Before recording: open http://127.0.0.1:8000/#/demo, sign in (top right) with the token in
   `~/recap-demo-take1/operator-token`, and choose a pace (**Quick**: about half a minute to the
   approval). Hide the terminal that printed the token, close other windows, turn on Do Not Disturb.
2. Press **Shift-Command-5**, choose **Record Selected Portion** around the browser window (or
   **Record Entire Screen**), set **Options** > microphone to **None** unless you narrate, **Record**.
3. Click **Start the guided demo**. When the dialog opens, show the action and **Why you are
   asked**, click **Approve this exact action**, then **Close and watch the run**, and wait for the
   outcome. For the other ending: **Restart**, then **Reject**.
4. Stop with the stop button in the menu bar (or **Command-Control-Esc**); trim it in QuickTime
   Player (**Edit > Trim**). Watch it once before sharing.

**Show:** the label **Recorded real-model execution — replayed locally** and its sentence ("no
model is called now") from the first second; the stepper advancing; the model lines marked
"replayed, not called now"; the decision card and the dialog; the outcome with the oracle's verdict.

**Never show:** the operator token (the terminal line that printed it, the `operator-token` file,
any command that prints it; Mission Control itself never renders it, and the smoke test checks
every page); anything presented as live inference (do not crop out the label, and do not caption,
cut or narrate the replay as a model working now: the demo is a replay and says so); home-folder
paths, other windows or notifications. Keep the video out of the repository.
