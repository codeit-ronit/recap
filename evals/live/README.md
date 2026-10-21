# Real-model runs, exported for checking

Each folder is one real RECAP run, exported with `scripts/export_run_trace.py` so that
anyone can check what happened:

- `trace.jsonl`: every trace event, in order;
- `run.json`: the final run record;
- `recording.jsonl`: every HTTP exchange with the model server (it replays offline);
- `README.md`: a readable account built only from those files and the approval log.

Every run used the **simulated** environment: nothing here touched a real system. Every run
cost $0. Each is one run of one scenario, so together they show that the real-model path
works, not how often a model is right.

| Folder | Model | Prompts | Approved by | Outcome |
|---|---|---|---|---|
| [`2026-10-21-qwen3-8b-human-approved-prompts-v4.2/`](2026-10-21-qwen3-8b-human-approved-prompts-v4.2/README.md) | `qwen3:8b`, local (Ollama 0.34.4, weights `sha256:a3de86cd…`) | prompts-v4.2 | **the owner, at an interactive terminal** (the log: "interactive terminal; answered after 175.9 s") | RESOLVED, oracle verified |
| [`2026-10-21-qwen3-8b-human-approved-prompts-v4.1/`](2026-10-21-qwen3-8b-human-approved-prompts-v4.1/README.md) | `qwen3:8b`, local (Ollama 0.34.4) | prompts-v4.1 | the owner, at the terminal (53.4 s after the request) | RESOLVED, oracle verified |
| [`2026-10-21-gpt-oss-120b-groq-free-auto-approved/`](2026-10-21-gpt-oss-120b-groq-free-auto-approved/README.md) | `openai/gpt-oss-120b` on Groq's Free plan | prompts-v4 | the simulation auto-approver | RESOLVED after an escalation |

## What each run shows

- **The v4.2 run is the M2 deliverable.** A real local model investigated through the MCP
  tool server, the owner approved its exact fix at the terminal, and the oracle verified the
  recovery. What the record itself proves is narrower: the answer was typed on an
  interactive terminal 175.9 s after the prompt appeared, a human pace. At that code version
  an answer typed in advance would have been read the moment the prompt appeared, so
  175.9 s rules it out (discarding typed-ahead input was added afterwards). It cannot prove
  who typed it. The model read health, the error-rate metric, deployments, events and logs
  (5 requests, 12,834 input / 724 output tokens), named release `v2.4.1` as the cause and
  proposed rolling back to `v2.4.0`. Since prompts-v4.2 no fixed prompt text holds either
  value (a test checks it for every scenario). The recording shows where they came from:
  neither appears in the first two prompts, and both first appear in the third, inside the
  result of the `get_recent_deployments` call the model chose ("checkout v2.4.1 (previous
  v2.4.0)").
- **The v4.1 run** was the first human-approved run. Its review confirmed real inference,
  the terminal approval and the recovery, and found that prompts-v4.1 used `"v2.4.1"` and
  `"v2.4.0"` as schema examples: exactly this scenario's answer. So that run cannot show the
  model read the versions from the evidence. That finding led to prompts-v4.2
  ([ADR 0019](../../docs/decisions/0019-checkable-evidence-of-a-real-model-run.md)). That
  RECAP version did not yet record whether the answer came from an interactive terminal.
- **The Groq run** shows the free hosted plan in use. Every one of its 8 responses carried
  `x-ratelimit-limit-requests: 1000`, the free plan's daily limit, which RECAP checks on every
  response. Two answers were rejected by Groq's strict-schema check (HTTP 400
  `json_validate_failed`): one was complete but left out two required (nullable)
  tool-call fields, and its corrective retry came back empty. Its prompts are prompts-v4, before the M2 review. Its
  exported recording keeps only the answer fields RECAP reads: Groq's request and answer IDs
  were removed. It does not replay: it has no committed code version, and its second round
  ran two specialists in parallel, whose prompts depend on the order they read evidence in
  (a replay stops with `ReplayMismatch` instead of guessing).

## The code version of each run

- v4.2 run: `689b1af0e6aa+dirty`. That is the commit plus uncommitted changes at the moment
  the run started: six documentation files and `scripts/export_run_trace.py`, all last
  changed at least 9 minutes before the run. The run executes neither docs nor the export
  script, so it ran exactly commit `689b1af`.
- v4.1 run: made before RECAP recorded code versions. File times match commit `da07ac3`,
  with no uncommitted change while it ran.
- The Groq run: made before RECAP recorded code versions, during M2 development.

## Replaying a run offline

```bash
git checkout <the run's commit>            # a replay needs the same prompts
RECAP_DATA_DIR=/tmp/recap-replay RECAP_USAGE_LEDGER=/tmp/recap-replay/ledger.db \
RECAP_PROVIDER=replay RECAP_REPLAY_RECORDING=evals/live/<folder>/recording.jsonl \
python scripts/demo.py --scenario bad-deployment --no-memory --approve auto
```

The replay answers every model request from the recording: no model is called, and the
run is labeled REPLAY. With different prompts it stops with `ReplayMismatch` instead of
guessing. The two `qwen3:8b` exports replay this way (checked with their own commits); a
run with a parallel round, like the Groq one, may not, because its specialists' prompts
depend on the order they ran in. A hosted recording would replay without a key, because
nothing is sent.

## Adding a run

```bash
set -a; . ~/.config/recap/groq.env; set +a     # optional: also checks your key's value
python scripts/export_run_trace.py <run_id> evals/live/<date>-<model>-<how-approved>
```

The export cleans every file, then refuses to write anything that still holds a secret
environment value, a key-shaped string, your login name or a local path.
`tests/unit/test_published_artifacts.py` scans every file here again in CI.
