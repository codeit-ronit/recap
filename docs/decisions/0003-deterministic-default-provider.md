# ADR 0003: Make a scripted, deterministic policy the default provider

**In one sentence:** by default, RECAP's agents are driven by a scripted policy that gives the
same answer for the same input, costs $0 and is **not an LLM**; a Claude adapter exists but
only runs if you opt in with your own API key.

## Status

Accepted.

## Context

A *provider* is the "brain" behind each agent. It implements the `LLMProvider` interface in
[recap/core/interfaces.py](../../recap/core/interfaces.py) with two calls: `next_action`
(which read-only tool to call next, or finish with hypotheses) and `propose_mitigation`
(draft one fix).

V1 needs:

- tests and continuous integration that run offline, with no secrets and no bills;
- demos and evaluations that give the same result every time;
- safety tests that show what the **gateway** does, not what a model happened to do that day;
- a way to plug in a real LLM later without changing the rest of the system;
- honest reporting: nobody should mistake a scripted baseline for an AI result.

## Decision

- **Default:** `deterministic` in [configs/providers.yaml](../../configs/providers.yaml),
  which builds `DeterministicProvider` from
  [recap/providers/deterministic.py](../../recap/providers/deterministic.py). Its name in
  traces is `deterministic-scripted-policy` and its kind is `scripted`. It follows role
  playbooks ([playbooks.py](../../recap/providers/playbooks.py)), applies fixed evidence
  heuristics ([heuristics.py](../../recap/providers/heuristics.py)), and reads the
  `Recommended action:` lines of runbooks
  ([recap/knowledge/hints.py](../../recap/knowledge/hints.py)). It makes no network calls,
  costs $0, and returns the same decision for the same request.
- **Same interface as an LLM.** It returns the same validated decision types
  (`InvestigationDecision`, `ProposalDecision`), so the harness cannot tell which provider
  is running.
- **Estimates are labeled as estimates.** Token counts come from `estimate_tokens` in
  [recap/context/tokens.py](../../recap/context/tokens.py) (about 4 characters per token).
  The cost label reads `$0.00 external API charged - token counts are estimates
  (deterministic mode)`, and the demo banner says "scripted baseline policy, NOT an LLM".
- **Optional Claude adapter.** `AnthropicProvider` in
  [recap/providers/anthropic_provider.py](../../recap/providers/anthropic_provider.py) is
  opt-in: you install the `llm` extra, set `RECAP_PROVIDER=anthropic`, and supply your own
  `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, with your own billing. If the SDK or the
  key is missing, `create_provider` in [recap/providers/factory.py](../../recap/providers/factory.py)
  raises `ConfigError` instead of quietly falling back. It is tested only with a fake
  client and is never called by the tests, CI or the demos.
- **Red-team wrapper.** `PromptInjectedProvider` in
  [recap/safety/redteam.py](../../recap/safety/redteam.py) wraps any provider to play a
  planner that was successfully prompt-injected. It exists to test the gateway.

## Consequences

### Good

- CI and the full test suite run offline and free.
- Runs are reproducible:
  `tests/e2e/test_demo_scenarios.py::test_runs_are_reproducible` checks it.
- Safety tests isolate the gateway: with a planner whose behavior is known exactly (including
  the red-team one), a blocked action is clearly the gateway's work.
- The whole pipeline (retrieval, memory, routing, approvals, oracle) can be exercised and
  measured end to end.

### Bad

- **Results say nothing about LLM agent performance.** Differences between evaluation
  configurations show how this scripted policy uses runbooks, memory and routing. They are
  not evidence about LLMs (see [evals/reports/latest/summary.md](../../evals/reports/latest/summary.md)),
  and the evaluation runner refuses any other provider, because the V1 benchmark and its
  caveats are defined for this policy.
- The policy parses the simulator's own log and event templates
  ([recap/core/signals.py](../../recap/core/signals.py)). On real logs it would misread or
  stay "insufficient". Real environments need real parsers or an LLM provider.
- "Learning" in V1 means verified episodic memory. No model is trained or fine-tuned.
- Token counts are estimates, not tokenizer counts, and the Claude adapter has not been run
  against the real API.
- Every document and report has to say "scripted policy, not an LLM", or readers will
  over-read the results.

## Alternatives considered

- **An LLM as the default.** It would cost money on every test run, need secrets in CI, and
  make safety tests flaky because the same input can give different outputs.
- **Recorded LLM replies replayed in tests.** This needs real recordings first, and the
  recordings go stale whenever a prompt changes.
- **A local open-weights model.** A large download, hardware requirements, and results that
  are still not exactly reproducible.
- **Hard-coded flows with no provider interface.** Simpler today, but there would be no
  clean place to plug in a real model later.
