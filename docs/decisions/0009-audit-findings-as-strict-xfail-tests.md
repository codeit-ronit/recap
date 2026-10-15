# ADR 0009: Reproduce every audit finding as a strict xfail test before fixing it

**In one sentence:** each defect found by the independent V1 audit first becomes a test
that asserts the correct behaviour and fails on the current code, marked
`xfail(strict=True)`; the fix then turns it green and removes the mark, so no finding can be
"fixed" without proof and no test can silently start passing.

## Status

Accepted (M0, 2026-10-15).

## Context

The audit of V1 found defects in six areas: context loss (2.1), forged evidence (2.2),
oracle and label leakage (2.3), approval races and API security (2.4), MCP execution
authority (2.5) and the recovery lifecycle (2.6). Several of them passed unnoticed because
the existing tests checked the wrong layer. For example, the scripted provider reads full
structured evidence, so no test ever looked at the text an LLM would actually be shown
(2.1). Fixing such code without a test that first fails would repeat the same mistake: the
fix could be wrong, or aimed at a different layer than the bug.

## Decision

- **One test file per audit item**, named `test_audit_<topic>.py`, under `tests/adversarial/`
  or `tests/e2e/`. Each file's docstring says, in plain words, what the audit found and how
  the tests reproduce it.
- **Reproductions assert the correct behaviour** and are marked
  `@pytest.mark.xfail(strict=True, reason="AUDIT-2.x: ...; fixed in M1")`. "Strict" makes an
  unexpected pass fail the suite, so the mark has to be removed in the same change as the
  fix.
- **Right-reason check.** Every reproduction is run once with `pytest --runxfail` to confirm
  it fails on its intended assertion and not on an import, fixture or timeout error. Strict
  xfail would otherwise hide a crash.
- **Guards** sit next to the reproductions: tests that already pass and pin the behaviour a
  fix must keep (for example, real OOM kills must still prove memory pressure, and an
  approved write must still reach the sandbox). They stop a fix from simply switching a
  feature off.
- **The real pipeline wherever the finding is end to end**: the real controller, MCP
  subprocess, gateway and simulator, with only the scripted provider wrapped to record what
  it is handed. Unit tests pin each faulty rule on its own.
- **Assertions are about observable behaviour**, not about the future implementation: LLM-visible
  text, HTTP status codes, stored decisions, the sandbox's own facts. The fix stays free to
  choose its design.
- **A fix never weakens a reproduction.** If a test turns out to encode a wrong expectation,
  the change must explain why, in the test file.

The results are recorded in [docs/status.md](../status.md): 43 reproductions and 11 guards.

## Consequences

### Good

- Each audit item has executable proof, before and after.
- The suite stays green during the work, and CI catches a fix that lands without its test
  being updated.
- Reviewers can read one file per finding and see exactly what changed.

### Bad

- Strict xfail can hide a test that fails for an unrelated reason; the `--runxfail` check
  is a manual step at each milestone.
- End-to-end reproductions are slower (the suite went from 30 s to 54 s).
- Some tests depend on the simulator's current scenarios; a change to a shipped scenario
  can require updating them.

## Alternatives considered

- **Fix first, add tests afterwards.** Faster, but nothing shows that the test would have
  caught the bug.
- **`pytest.mark.skip` instead of xfail.** A skipped test never runs, so it cannot report
  that a fix landed or that the bug is still there.
- **Plain failing tests on a branch.** Breaks CI for everyone until every fix lands.
