# ADR 0019: A real-model run is evidence only if a reader can check it

**In one sentence:** RECAP's fixed prompt text may hold no scenario's answer, a console
approval must record how and when it was typed, every run names the code and the model
weights it used, and a run is shared only through an export that is complete and refuses to
write anything that points at the owner's machine or accounts.

## Status

Accepted (M2, 2026-10-21), after the independent review of the first human-approved run
(`run-4b3d3f1704`). Amends [ADR 0016](0016-zero-cost-llm-providers-and-a-constrained-step-protocol.md)
(prompts-v4.2), [ADR 0015](0015-operator-auth-and-no-wins-approvals.md) (console approvals
need a terminal) and [ADR 0018](0018-record-every-model-exchange-and-replay-in-ci.md)
(what a recording keeps).

## Context

Six independent reviewers checked the first human-approved run and its export. They
confirmed real local inference (the trace, the recording, the usage ledger and Ollama's own
log agree request by request), a decision at the terminal 53 s after the request, and
oracle-verified recovery. They also found that the evidence claimed more than it proved:

- prompts-v4.1's schemas used `"v2.4.1"` and `"v2.4.0"` as examples: exactly the
  `bad-deployment` scenario's bad release and rollback target. The model also read both in
  the deployment evidence, but the run could not tell reading from copying;
- the actor `console-user` only proved that `input()` returned "y": piped input would have
  produced the same record;
- the record named the model by its tag (`qwen3:8b`) and the code by nothing;
- the export's README said "tools mode" for a proposal answered in JSON-schema mode and
  "a person answered" from the actor's name alone, and its own safety gate only checked
  secret environment values;
- recordings kept Groq's `x-request-id`, which could link a published recording to the
  owner's account.

## Decision

1. **Neutral fixed text.** Prompts, instructions, schemas and tool definitions carry no
   example value (prompts-v4.2 and prompts-v3.2 say "exactly as the deployment evidence
   names it" or use a `<placeholder>`). `tests/unit/test_prompt_neutrality.py` collects every
   scenario's versions and the longer texts of its faults, deployments, planted log lines
   and expected answer, and fails if any appears in any fixed text.
2. **Attested console approvals.** `ConsoleApprovalChannel` reads an answer only from an
   interactive terminal; piped or missing input is rejected unread. Anything typed before
   the prompt appeared is discarded, so the answer is typed after the proposal was shown.
   The append-only decision log records how the answer came (the deliverable run: "interactive
   terminal; answered after 175.9 s"; flagged when faster than a person can read), via
   `ApprovalStore.decide(note=...)`, also when the answer is a "no" that revokes an earlier
   approval; the `approval.decided` trace event carries it. `demo.py --approve ask` refuses
   to start without a terminal. This proves a terminal and a timing, not a person.
3. **Provenance on every run.** `versions.code_version` is the git commit (with `+dirty` when
   tracked files have uncommitted changes). `ProviderInfo.server` holds what the model server
   reported before the first request: Ollama's version, the model's capabilities, context
   window, size, quantization and the digest of its weights file (from the `FROM` line of
   `/api/show`). A recording keeps only the weights file's name, never its local path.
4. **A complete, conservative export** (`scripts/export_run_trace.py`): the trace, the run
   record, the model recording, and a README built only from them and the approval decision
   log: each request's mode, the policy verdicts with the deciding rule, the write's effects,
   each approval with its own note and wait, the oracle's before and after, the free-plan
   header values of a hosted run, and a scope and caveats section. Model text in the README
   is escaped, so it cannot pose as one of RECAP's lines. The export cleans first
   (redaction, the data folder becomes `data`, the home folder `~`, no request or answer IDs),
   then checks every decoded and URL-decoded string and key, not the JSON text, and writes
   nothing at all if one still holds a secret environment value, a key-shaped string, the
   login name or a local path, or if a model run's recording is missing. Files are staged
   and moved into place; an existing export is replaced only with `--replace`.
5. **CI scans what is published.** `tests/unit/test_published_artifacts.py` checks every
   committed recording and export for key shapes, local paths and request IDs, with boolean
   assertions so that a failure never prints the matched value into a public log.

## Consequences

### Good

- A reader can check each claim: which code and which weights ran, who approved and how,
  exactly what the model was sent and answered (the recording replays offline), and that no
  answer was planted in RECAP's own text.
- The replay fixture re-recorded with prompts-v4.2 resolved the same way: the model still
  named `v2.4.1` and proposed rolling back to `v2.4.0`, values that now appear only in tool
  evidence (`v2.4.1` in the deployment history and the error logs, `v2.4.0` only in the
  deployment history).
- Each new guard has a test that fails when the guard is undone (17 revert checks after the
  first review, 22 after the second, all caught).

### Bad

- "A person approved" remains an inference: a terminal and a human-scale delay, not an
  identity, and a program driving a terminal can still answer. Recording the OS user or
  host name would put personal data into public exports.
- An export replays only with the prompts it was recorded with; every prompt change needs a
  re-recorded fixture and makes older recordings stop with `ReplayMismatch`.
- The gates can refuse legitimate content (for example a login name that is also a common
  word). The export then stops and names the file and the kind of finding.

## Alternatives considered

- **Keeping realistic example values.** They help small models format answers, but any
  concrete value can be some scenario's answer; placeholders and field descriptions are
  enough (the v4.2 fixture run needed no correction).
- **An OS-level identity in the approval record.** Rejected for public exports; a signed
  approval (for example a one-time code) is the better step once real targets exist.
- **Only a CI scan.** Kept as the second line; the export's own gates stop a bad file before
  it exists.

## Amended after the second review

Three more reviewers (security, correctness, test quality) checked the first version of these
fixes and proved these gaps, each now closed with a test that fails when the fix is undone:

- the export's last gate searched the JSON text, where a key after a newline sits behind
  `\n`; it now checks decoded strings and dict keys, and URL-decoded forms, and compares
  paths case-insensitively. The redactor learned the key formats it missed (OpenAI project
  keys, all GitHub token kinds, Slack tokens, temporary AWS keys, private-key blocks);
- a "y" typed while the model worked was taken as the answer the moment the prompt appeared;
  typed-ahead input is now discarded and very fast answers are flagged;
- a console "no" that revoked an approval lost its note, and the README could attribute one
  approval's note to another; notes and waits now come from each proposal's own deciding
  log entry;
- Groq also puts its request ID in the answer body (`x_groq`), with the answer's own `id`;
  recordings keep only the fields the provider reads;
- model text in the README could forge an approval line; it is now escaped;
- re-exporting into a folder left the previous export's files behind; exports are staged;
- the weights digest is read only from the modelfile's `FROM` line, and `code_version` is
  "unknown" for a copy inside another git repository;
- the prompt-neutrality guard scanned only one role's first prompt; it now scans every role
  at its first and last step, the proposal prompts, every string literal of the prompt
  modules (feedback included) and the runbooks, catches pieces of facts (class names,
  dotted identifiers) and quoted example services, and a self-test plants a fact in each
  kind of source. It found one more planted example (`{"service": "checkout"}` in the paid
  adapter's prompts-v3.1, now prompts-v3.2).
