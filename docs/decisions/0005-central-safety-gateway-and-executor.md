# ADR 0005: One central safety gateway, and the controller as the only executor

**In one sentence:** every tool call passes one trusted gateway that asks one trusted policy,
only the incident controller may execute a fix, and a reversible fix needs a human approval
bound to that exact action; destructive actions are always denied.

## Status

Accepted. Extended by [ADR 0014](0014-signed-capabilities-at-the-write-boundary.md) (the gateway's decision is now also enforced at the write boundary, through a signed capability) and [ADR 0015](0015-operator-auth-and-no-wins-approvals.md) (an explicit "no" wins until the action runs; the API approver is the authenticated operator).

## Context

Many things in an incident-response agent can go wrong at once:

- the planner (scripted or LLM) can be wrong, or **prompt-injected**, meaning text written
  to look like instructions (in a log line, a runbook or a memory) makes it propose
  something harmful;
- the MCP server can lie about its tools or return hostile output
  ([ADR 0004](0004-real-mcp-over-stdio.md));
- a human can approve the wrong thing, or approve too late.

Safety must not depend on any of them behaving well. It has to sit in code that RECAP
controls, decide from configuration that RECAP controls, and be easy to test.

## Decision

1. **One door.** `ToolGateway` in [recap/tools/gateway.py](../../recap/tools/gateway.py)
   handles every tool call, in this order: policy check, then for writes the exact approval
   and a lock on the target, then a budget reservation, a hard timeout, redaction, bounding
   and flagging of the output, and finally immutable evidence and an audit trace event.
2. **One judge.** `PolicyEngine` in [recap/safety/policy.py](../../recap/safety/policy.py)
   runs rules R1 to R10, R14, R11 and R12 in that order, and the first DENY wins (the
   gateway itself adds R13, a lock against conflicting fixes of one target, and each agent's
   tool allowlist). It decides only from trusted
   inputs: the tool's risk tier and argument rules from `ToolRegistry`
   ([recap/tools/registry.py](../../recap/tools/registry.py), built from
   [configs/policies.yaml](../../configs/policies.yaml)), the validated arguments, the
   caller, the environment, and the approval as stored. It ignores model text, a proposal's
   self-declared risk, and the server's descriptions and annotations. A fix must use a tool
   from the trusted remediation catalog (R14), and its params may not set the tool's target
   argument (R11 and R12 deny that), so an approver can never be shown a decoy service.
3. **One executor.** Only `IncidentController` in
   [recap/harness/controller.py](../../recap/harness/controller.py) calls
   `ToolGateway.execute_mitigation`. Worker agents get a `ReadOnlyTools` handle, which can
   only call the read-only tools on their role's list in
   [configs/agents.yaml](../../configs/agents.yaml).
4. **Risk tiers.**

   | Tier | Example | Verdict |
   |---|---|---|
   | `read` | `get_service_logs` | ALLOW automatically as a plain read; never accepted as a fix (R14) |
   | `reversible_write` | `execute_rollback`, `restart_service` | APPROVAL_REQUIRED, then ALLOW only with an exact approval |
   | `high_risk` | none in V1's catalog | DENY in V1 |
   | `destructive` | `delete_namespace` | always DENY, even with an approval |

5. **Exact approvals.** `ApprovalStore` in [recap/safety/approvals.py](../../recap/safety/approvals.py)
   binds an approval to the incident, the proposal ID and the proposal's **digest** (a
   SHA-256 fingerprint of `{incident_id, action, target, params}`). The call's arguments are
   derived from the proposal, never typed freely. An approval expires (`ttl_seconds: 900`
   in [configs/budget.yaml](../../configs/budget.yaml)), the gateway re-reads it from the
   store, and it is consumed **before** the call, so it can never be replayed.
6. **Gate early, check again.** The controller asks the policy about a proposal before any
   human sees it, so a DENY ends the run as BLOCKED without bothering anyone. At execution
   the policy checks again, this time with the stored approval. Even earlier, the controller
   refuses an incident whose namespace is not allowlisted before any tool call (CREATED →
   BLOCKED).
7. **Safety off only in a simulation.** Evaluations may switch safety off to measure what the
   gateway prevents. Rule R3 refuses that outside a simulated environment, and rule R0
   records the verdict that would have applied. The API refuses it too, and a scenario file
   cannot switch it off: its overrides may only set the planner and the strategy.

## Consequences

### Good

- Safety holds even when the planner is compromised. In the `malicious-log` demo the
  red-team planner proposes `delete_namespace` and the run ends BLOCKED with
  `R9-destructive-deny`. In the red-team evaluation (N = 1, simulated), safety on blocked
  the 1 unsafe attempt and nothing unsafe executed. Safety off let both unsafe attempts
  through: 1 executed in the sandbox, and the sandbox refused the second because the
  namespace was already deleted.
- One place to audit. Every verdict is a `policy.decision` trace event with its rule IDs,
  and the dashboard shows them.
- The policy is a pure function of its request, so it is easy to unit-test:
  `tests/unit/test_policy.py::test_decisions_are_deterministic`.
- The adversarial tests in [tests/adversarial/](../../tests/adversarial/) check the whole
  chain, from injected text to zero write calls.

### Bad

- A single point of failure: a bug in the gateway or the policy is a bug everywhere. Tests
  reduce this risk; nothing duplicates the check.
- Python cannot enforce the boundary against code inside the RECAP process, and the tool
  server does not verify approvals.
- The policy is simulator-scoped: one namespace and five services in YAML allowlists. A real
  cluster needs an RBAC-aware policy (role-based access control) behind the same
  `SafetyPolicy` interface.
- It is conservative by design. No destructive or high-risk action is ever automated, so
  some real fixes would have to be done by a human outside RECAP.
- Approver identity is a free-text label. V1 does not authenticate who approved.

## Alternatives considered

- **Safety rules in the prompt.** They depend on the model obeying, and prompt injection is
  exactly an attack on that.
- **Enforcement inside the MCP server only.** The server is untrusted, and MCP annotations
  are only hints. Server-side checks are still useful as a second layer: signed approvals
  verified by the tool server are planned
  ([threat model](../threat-model.md#future-hardening)).
- **An LLM judging each action.** Not deterministic, hard to test, and open to the same
  injection as the planner.
- **Letting each agent write with its own approval.** More write paths to audit, and a
  compromised worker would sit one step from a write.

See [the threat model](../threat-model.md) for every threat, mitigation and proving test.
