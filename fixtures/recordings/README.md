# Recorded model runs (replay fixtures)

Each `.jsonl` file is a recording of a **real** language-model run: a header line, then
every HTTP exchange the provider made, in order (recap/providers/transport.py). The replay
provider (`RECAP_PROVIDER=replay`) answers from a recording instead of a model, so CI can
run an authentic model-driven incident offline, with no network and no model.

A replay is always labeled `REPLAY` (origin `replay`): its answers were written by the
model in the original run, not now. A request that is not in the recording stops the run
with `ReplayMismatch`; nothing is ever guessed. Requests are matched by what they ask (a
fingerprint of the request with run-specific IDs normalized), never by their position, so
two specialists that ask in another order still get their own answers.

## The manifest: every recording is pinned by its bytes

[`MANIFEST.json`](MANIFEST.json) lists each recording under an id, with the SHA-256 and
size of its bytes, the run it reproduces (recording ID, scenario, routing, provider, model,
prompt version) and a title and description written by a person. RECAP uses it in three ways
([recap/providers/recordings.py](../../recap/providers/recordings.py)):

- **Replay by id.** A run with `provider: "replay"` and `replay_fixture: "<id>"` (in its
  `RunConfig`) replays that recording. RECAP reads the file once, checks its SHA-256 against
  the pin and parses exactly those bytes. A file with one changed byte is refused before any
  request is answered ("recording '<id>' was altered"); so is an unknown id. A client names a
  recording only by its id, never by a path.
- **Provenance.** A replay's `provider_info.replay_of` says what it answers from: `sha256`
  (the exact bytes), `fixture` (the id of the committed recording with those bytes, or
  `null` when they are not one), `recording_id`, `model`, `prompt_version` and
  `original_origin` (what made the recording, e.g. `local_llm`). `provider_info.recording`
  and `versions.recording` give the file repo-relative, or only its name when it is outside
  the repository: never a local folder. A replay that carries a `fixture` is a recorded
  real-LLM run against the simulated target, and the API and dashboard can label it so.
- **CI.** `tests/unit/test_recordings_manifest.py` runs the same check as
  `python scripts/recordings_manifest.py --check` on every push: each pin and size, each
  header fact, each export folder, and no recording in this folder without an entry.

Scripts and tests can still replay any file by path (`RECAP_REPLAY_RECORDING=<file>` with
`RECAP_PROVIDER=replay`). Such a replay is labeled with a `fixture` only when its bytes are
exactly a committed recording's: a copy of a fixture is that fixture, an edited copy is not.

### After re-recording a fixture on purpose

1. Record the run again (below) and copy the new file over the old one.
2. `python scripts/recordings_manifest.py --update` re-pins the SHA-256 and size of every
   listed file, copies the recording ID, provider, model and prompt version from its header,
   and prints each change.
3. Review the changed entry's title, description, `agents` and `approved_by`: they describe
   the run that was recorded, and `--update` never rewrites them.
4. `python scripts/recordings_manifest.py --check` passes; commit the recording and the
   manifest together.

Never use `--update` to make an unexpected `--check` failure go away: an unexpected change
means a recording was altered. A new recording needs an entry written by hand.

## What a replay does and does not do

- It answers the **model's** requests only. Everything else runs now: the controller, the
  MCP tool server, the safety gateway, the simulator, the evidence verifier and the oracle.
- It asks for a **fresh approval**. `approved_by` says who approved the original fix, in the
  original run. That approval was bound to that run's proposal and digest, was used once and
  has expired; the replay's proposal is a new one (another incident, another digest), so a
  person decides again (in tests, a test approver). Approving comes after the model's
  proposal and changes nothing the model saw, so it does not change what is replayed.
- It **never acts on a real system**. The controller refuses a replay before it starts when
  the configured environment is not simulated: no recording is read, nothing is reset and no
  approval is asked for (`tests/e2e/test_golden_replay.py`).
- It writes **no experience memory**: the experience was the original run's.

## The recordings

| Id | File | Model | Recorded | What happened |
|---|---|---|---|---|
| `qwen3-8b-unfamiliar-parallel` (the golden demo) | `ollama-qwen3-8b-unfamiliar-parallel.jsonl` | `qwen3:8b` (Q4_K_M, 8.2B, weights `sha256:a3de86cd1c13…`) via Ollama 0.34.4, `tools` mode, prompts-v4.7 | 2026-10-25 (recording `20261025-121828-ollama-6a5cfd`, exported in [`evals/live/2026-10-25-qwen3-8b-human-approved-unfamiliar-prompts-v4.7/`](../../evals/live/2026-10-25-qwen3-8b-human-approved-unfamiliar-prompts-v4.7/README.md), byte for byte the same file) | The M3 deliverable: `m3-inventory-leak-behind-frontend` (held out), forced parallel routing, the fix approved by the owner at an interactive terminal. Two specialists in both rounds (telemetry 6 reads, infrastructure 10). Round 1: telemetry's two dependency-failure claims were supported but not actionable; infrastructure's claim that cart's recent deployment broke frontend was not accepted (it cited none of the evidence its rules relied on, V1-grounding). Round 2 got the verifier's leads ("cart's calls to inventory fail ... find out what is wrong with inventory"); the verifier supported resource pressure on inventory; the model proposed `restart_service` on inventory; the oracle verified recovery. 14 model requests, 52,585 input / 2,394 output tokens, about 11 minutes live. A replay of it took 0.95 to 1.07 s with no simulated latency and 2.2 to 2.3 s with the configured latencies (5 runs each on the same M3 MacBook Air, the approval answered at once by a test approver). |
| `qwen3-8b-bad-deployment` | `ollama-qwen3-8b-bad-deployment.jsonl` | `qwen3:8b` (as above) via Ollama 0.34.4 on an M3 MacBook Air with 16 GB, `tools` mode, `think: false`, prompts-v4.7 | 2026-10-25 (recording `20261025-110603-ollama-b1ae62`) | `bad-deployment`, `use_memory: false`, BM25 retrieval, adaptive routing, simulation auto-approver (the approval channel does not change what the model sees). One investigator asked for 6 read-only tool calls in 5 steps (health; deployments; logs; events; the error-rate and latency metrics); it was offered `search_runbooks` and did not use it. It then claimed a `bad_deployment` of `checkout` `v2.4.1`, citing 5 evidence items (health, deployments, logs and both metrics), among them all 3 its rules relied on (V1-grounding: "cites 3 of the 3"). The verifier supported it; the model proposed `execute_rollback` to `v2.4.0`, the release before the bad one (VP4); the oracle verified recovery. 7 model requests, 19,946 input / 867 output tokens, about 3.5 minutes. Earlier fixtures: `20261025-100522-ollama-e200b7` (v4.6), `20261021-144429-ollama-a118d0` (v4.5), `20261021-141657-ollama-874b7e` (v4.4), `20261021-134004-ollama-5f0326` (v4.3), `20261021-091407-ollama-33e789` (v4.2). |

The golden demo's walkthrough is [docs/demo-walkthrough.md](../../docs/demo-walkthrough.md).

## What a recording keeps

The request bodies (prompts are built from simulated incident data only), the responses
(Ollama's `/api/show` trimmed to capabilities, size, context length and the weights file's
name, never its local path; a hosted chat answer only the fields the provider reads), and
the rate-limit headers. No request or answer IDs are kept. It never holds an API key: keys
are added only on the wire, and every line is scrubbed for secrets and the user's home
folder before it is written. The two committed recordings were checked again at the start
of M4 (no key shapes, no `Bearer`, no local paths, no machine, login or personal name).

## To record a new one

Run an LLM provider (`python scripts/demo.py --scenario bad-deployment --provider ollama
--no-memory`), copy its recording from `data/recordings/` here, add a manifest entry by hand
(or `--update` an existing one, above), and run `--check`. Re-record whenever the prompts
(now `prompts-v4.7`), the schemas or the tool catalog change: the replay tests fail loudly
when a recording no longer matches.
