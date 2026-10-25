# ADR 0028: Committed recordings are content-addressed and replayed by manifest id, never against a real system, and always with a fresh approval

**In one sentence:** a replay of a committed recording is chosen by its id in a manifest that
pins the file's SHA-256, answers only from the bytes that matched the pin, says which
committed recording those bytes are, is refused before it starts when the environment is
not a simulation, and asks a person for a fresh approval instead of reusing the recorded
one.

## Status

Accepted (M4, 2026-10-26). Extends [ADR 0018](0018-record-every-model-exchange-and-replay-in-ci.md)
(recordings and replays), [ADR 0019](0019-checkable-evidence-of-a-real-model-run.md)
(checkable evidence) and [ADR 0026](0026-a-replay-stops-where-its-recording-stopped.md)
(stops in replays). Builds on [ADR 0015](0015-operator-auth-and-no-wins-approvals.md)
(exact, single-use approvals) and [ADR 0007](0007-replaceable-environment-adapter.md)
(the environment adapter says whether it is simulated). The API side (starting a replay by
manifest id, `GET /replays`, the run labels) is
[ADR 0027](0027-a-typed-read-only-api-over-the-run-records.md).

## Context

The M3 deliverable is a recording of a real two-agent run (`qwen3:8b`, owner-approved,
oracle verified). Until M4, CI replayed it by file path, set for the whole process in
`RECAP_REPLAY_RECORDING`. M4 makes it the golden demo: a reviewer starts it with one click
in the dashboard. That raised five questions.

1. **How does a client choose a recording?** Letting a browser send a file path would let it
   point the server at any file it can read, and a path says nothing about what is in the
   file.
2. **How does a reviewer know that the replayed bytes are the committed real run?** One
   changed byte in a recorded token count changes a published number; one changed byte in a
   model answer changes a decision. A file name, a path or the header's recording ID all
   survive such an edit.
3. **What stops a replay from acting on something real?** A replay re-runs decisions a model
   made about another system at another time. Today the only environment is the simulated
   shop, but M5 brings real adapters (Kind, SREGym), whose `is_simulated` is false.
4. **The original fix was approved by the owner. Should the replay reuse that approval?**
5. **Does the replay of a parallel run depend on timing?** ADR 0018, 0020 and 0026 say it
   does not; the golden recording was not yet tested for it.

A smaller problem came up on the way: a replay's run record showed its recording path as
given, so an absolute `RECAP_REPLAY_RECORDING` put a local folder (and so the user's name)
into `provider_info.recording`, `versions.recording` and any API answer that serves them.

## Decision

1. **A manifest pins every committed recording**
   ([fixtures/recordings/MANIFEST.json](../../fixtures/recordings/MANIFEST.json),
   [recap/providers/recordings.py](../../recap/providers/recordings.py)): an id, a file name
   in that folder (never a path), the file's SHA-256 and size, the facts the recording's
   header repeats (recording ID, provider, model, prompt version), how the original run was
   configured (routing, memory, retrieval) and a description written by a person. Every
   entry is a recording of a real model run. `check_manifest` verifies every pin and size,
   parses every file as a replay would, compares the header facts, checks the export folder
   and reports any recording without an entry; CI runs it
   (`tests/unit/test_recordings_manifest.py`), and so does
   `python scripts/recordings_manifest.py --check`. After a deliberate re-recording,
   `--update` re-pins hashes and sizes and copies the header facts; it never writes prose
   and never adds an entry.
2. **Replay by manifest id.** `RunConfig.replay_fixture` names a recording by its id; the
   runtime (`Runtime.make_provider`) passes it to `create_provider(..., replay_fixture=...)`
   ([recap/providers/factory.py](../../recap/providers/factory.py)), and the golden replay
   test fails if it ever stops doing so (the configured default recording would answer
   instead, and the run would end in `ReplayMismatch`). `read_fixture` refuses an
   unknown id, reads the file once, checks the SHA-256 of those bytes against the pin and
   returns them; the replay parses exactly those bytes, so nothing can change between the
   check and the replay. An altered file is refused (`ConfigError`: "recording '<id>' was
   altered", naming the pin, never a folder) before a replay transport exists, so no request
   is ever answered from it. Only the replay provider takes a fixture: any other provider
   given one is refused, never quietly ignored. `RECAP_REPLAY_RECORDING` and the replay
   entry in `configs/providers.yaml` keep working for scripts and tests; a run's own
   `replay_fixture` wins over them.
3. **Identity by content.** A replay's `provider_info.replay_of` now also holds `sha256` (the
   exact bytes it answers from), `fixture` (the manifest id whose pin equals that hash, or
   `null`) and `original_origin` (what made the recording, such as `local_llm`). Only the
   hash decides: a copy of a fixture anywhere is that fixture, and a copy with one changed
   byte is not, even though its header still names the original recording ID. A label such
   as RECORDED REAL-LLM RUN must rest on `fixture`, never on the header's recording ID.
   `provider_info.recording`, `versions.recording` and "recording not found" messages show
   the file repo-relative, or only its name outside the repository (`transport.shown_path`).
4. **A replay never acts on anything that is not simulated**
   ([recap/harness/controller.py](../../recap/harness/controller.py),
   `_check_replay_scope`). The controller refuses a replay before it starts when the
   environment is not simulated: first by the run's configuration (the replay provider, or a
   `replay_fixture`) before the provider is built, so no recording is even read; then by
   what was built (`origin == "replay"`), whatever the provider is called. The environment is
   never reset, no run record exists and no approval is asked for. A scripted run against
   such an environment is not affected by this rule (the auto-approver stays
   simulation-only, as before).
5. **A fresh approval, every time.** A recording holds model exchanges only; approvals are
   RECAP's own state and are never recorded or replayed. The replay's proposal is a new
   one, with its own proposal ID, incident and digest, so it goes through the same chain as
   any run: APPROVAL_REQUIRED (`R11-reversible-approval`), a person's decision bound to that
   exact digest, ALLOW only for that approval (`R12-approval-binding`), a single-use signed
   capability at the write boundary, then the oracle. The manifest's `approved_by` records
   who approved the original fix, as history only.
6. **Timing does not change the golden replay, and CI proves it**
   (`tests/e2e/test_golden_replay.py`). The golden recording is replayed with one
   specialist's tool calls held back (a slow tool-server path for that agent), then the
   other's: the agents finish round 1 in opposite orders, yet every finding, verdict,
   proposal, fix, oracle result, read and budget count equals the replay without a delay,
   and no specialist's context ever holds the other's evidence from the current round
   (ADR 0020).

## Why a fresh approval instead of the recorded one

- **The recorded approval cannot bind.** It names the original run's proposal and digest;
  the replay's proposal is another object. Accepting it would mean that one approval,
  given once for one run, authorizes a write in another run, which is exactly what exact,
  single-use, expiring approvals exist to prevent (ADR 0015). It was also used once and has
  expired.
- **A recording is evidence of what a model said, not of what a person decided now.** The
  person who approved then saw that run's evidence on that day. A replay's environment is a
  new simulation; only the person in front of it can decide about it.
- **It costs the replay nothing.** Approving comes after the model's proposal and changes
  nothing the model saw, so the replay asks the recording exactly what the original run
  asked. CI uses a test approver; the dashboard demo asks the operator, which is what the
  demo is meant to show.
- **The alternative, auto-approving every replay,** would make the demo skip the very step
  it exists to demonstrate, and would turn "replay" into a way around approvals the day a
  replay could reach a real system, which decision 4 forbids anyway.

## Consequences

### Good

- The dashboard can offer one-click replays without ever letting a client name a file.
- A reviewer can check that a run labeled as the recorded real run answered from the
  committed bytes: `replay_of.sha256` equals the pin in git. An edited copy is never
  labeled so; CI shows a one-byte edit in a token count being refused by id, and replayed by
  path only as an unlabeled replay with a token total the real run never reported.
- M5 can add real adapters without a new safety rule for replays: a replay is refused
  there by construction (CI tests it with a stand-in adapter whose `is_simulated` is false).
- Replay records no longer carry a local folder.
- Revert checks (13): each of 12 safeguards was undone on its own, and the two guard checks
  together (the guard by configuration, the guard by what was built, the hash check,
  parsing the checked bytes, the fixture in the provenance, the fixture winning over the
  variable, a fixture refused for other providers, no local paths, and the header-fact,
  unlisted-file, duplicate-id and file-name checks); its guarding tests failed every time,
  and the files were restored.

### Bad

- A deliberate re-recording now needs `--update` and a person's review of the entry's prose.
- Identity by content is all or nothing: an export that cleans a recording differently is a
  different file and is not recognized as the fixture.
- Replays are refused against every non-simulated environment, including a read-only replay
  that could help debug a real adapter's tools. That is deliberate; such debugging should
  record a new run instead.
- That every manifest entry is a real model run is checked by review, not by code: a
  recording cannot prove where its answers came from (its server version and weights digest
  are what a reviewer can check against the export).

## Alternatives considered

- **Paths from clients.** Refused: arbitrary file reads, and a path is not an identity.
- **Trusting the file name or the header's recording ID.** Refused: both survive an edit.
- **Signing recordings.** Not needed: the manifest in git is the trust anchor, and its hash
  is checked at use; a signature would add a key to protect and rotate.
- **Hashing only in CI.** Refused: the file is read again at replay time, so it must be
  checked then too, and the replay must use the bytes that were checked.
- **Reusing the recorded approval, or auto-approving replays.** Refused (above).
- **Letting replays run against real systems with an approval.** Refused: the decisions
  were made about another system; a person would be asked to approve a stale plan.
