# `recap/safety` — the trusted rules, exact approvals and red-team tools

RECAP's safety does not depend on the planner (scripted or LLM) behaving well. The tool
gateway asks the **policy engine** about every call, and the policy decides only from trusted
inputs: the registry's risk tier (from `configs/policies.yaml`), validated arguments, the
caller, the environment and the approval *as stored* in the ApprovalStore. Model text, a
proposal's self-declared `risk_level`, tool descriptions and server annotations are ignored.

| File | What it does |
|---|---|
| `policy.py` | `PolicyEngine` — ALLOW / APPROVAL_REQUIRED / DENY with rule IDs. |
| `approvals.py` | `ApprovalStore` (SQLite): the approval state machine, single use, the append-only decision log; approval channels (auto / console / external). |
| `capabilities.py` | `CapabilityIssuer` and `verify_capability`: signed, single-use, short-lived permission for one exact write, checked by the environment itself. |
| `injection.py` | Pattern-based prompt-injection **flagging** for untrusted text. |
| `redteam.py` | `PromptInjectedProvider` — a deliberately compromised planner (red team only). |

## Policy rules

Rules run in the order of this table and the **first DENY wins**. Every `SafetyDecision.rule_ids`
lists each rule that was evaluated, in order, so the last ID is always the rule that produced
the verdict. R14 was added after the gateway's R13, so its number is out of order. It runs after
the risk-tier rules, which keeps R9 as the reason a destructive proposal is denied.

| ID | Rule | Outcome when it fires |
|---|---|---|
| `R1-tool-name-format` | name must fully match `[a-z][a-z0-9_]{2,63}` (blocks case, whitespace, `;`, Unicode look-alikes) | DENY |
| `R2-known-tool` | tool must be in the trusted registry; advertised tools are not trusted | DENY |
| `R3-safety-scope` | safety may be switched off only in a simulated environment | DENY outside simulation |
| `R0-safety-disabled-simulation` | simulation-only ablation: allow, and state the verdict that *would* have applied | ALLOW |
| `R4-args-valid` | strict argument validation (unknown args, exact types, bounds, version format) | DENY |
| `R5-namespace-allowlist` | the environment's namespace and any namespace argument must be allowlisted | DENY |
| `R6-service-allowlist` | services must be allowlisted (enforced inside R4; recorded for the audit trail) | DENY via R4 |
| `R7-worker-read-only` | only the controller may use a non-read tool | DENY |
| `R8-read-allow` | a plain read-only call is allowed automatically; a read tool proposed as a mitigation is not (it goes on to R14) | ALLOW |
| `R9-destructive-deny` | destructive actions are never automated, regardless of model or document text | DENY |
| `R10-high-risk-deny` | high-risk actions are denied in V1 | DENY |
| `R14-remediation-catalog` | a mitigation (a request with a proposal) must use a tool from `remediation_tools` in `configs/policies.yaml`; without it, a read tool proposed as a "fix" would skip the approval gate | DENY |
| `R11-reversible-approval` | a reversible write must come from a proposal that matches the request and whose params do not set the target argument (else DENY); with no approval yet it waits for one | APPROVAL_REQUIRED / DENY |
| `R12-approval-binding` | the stored approval must match this exact proposal (see below) | ALLOW / DENY |

Rules enforced by the gateway itself (`recap/tools/gateway.py`):

| ID | Rule | Outcome |
|---|---|---|
| `R-agent-tool-allowlist` | a worker may only call the tools listed for its role in `configs/agents.yaml` | DENY |
| `R13-conflicting-remediation` | another fix for the same `scope/namespace/target` is in progress | DENY |

## Approval binding (R12)

An approval is never general consent. It allows **one** action, once:

* **Bound** to the incident, the proposal ID and the proposal **digest** — a SHA-256 of
  `{incident_id, action, target, params}`. Changing any parameter after approval breaks it.
* The request's tool and arguments must be exactly the ones derived from the approved proposal.
* **One target.** The proposal's target fills the tool's service/namespace argument, and its
  params may not set that argument too (denied at R11 and again at R12). Otherwise the approval
  card could show `params: {service: payment}` while `checkout` is the service changed.
* **Expires** (`configs/budget.yaml` → `approvals.ttl_seconds`, 15 minutes by default). A waiter
  that times out marks the request EXPIRED, so a late "yes" cannot be applied.
  `ApprovalStore.expire(proposal_id)` does the same on demand (the controller calls it when a
  run is cancelled while waiting) and wakes the waiter, which returns None. Once a request is
  decided, `expire()` changes nothing.
* **Single use**: the gateway consumes the approval *before* calling the tool, with an atomic
  SQL update (`... WHERE consumed_at IS NULL`), so it cannot be replayed even if the call fails.
* **Re-read from the store** right before execution, so a forged or edited `Approval` object
  held by the caller has no effect.
* Every channel records its decision through `ApprovalStore.decide`, which checks the digest
  and that the request has not expired.
* **"No" wins until the approval is used** (audit finding 2.4). An approval is accepted only for a
  pending request. A rejection is accepted for a pending request **and** for an approved one
  whose approval was not used yet (a *revocation*: the gateway's later attempt to use it is
  refused). After a rejection nothing more is accepted. The gateway uses the approval
  immediately before it sends the write; from then on a rejection is refused ("already used
  to send the action"). V1 let a separate API approval override the operator's `n`.
* **Atomic across processes.** Every transition is a compare-and-swap inside one SQLite
  `BEGIN IMMEDIATE` transaction, so the API server and the CLI sharing `data/recap.db` are
  serialized: a revocation and the gateway's use of the approval can never both win.
* **Append-only decision log.** `approval_events` records every request, approval,
  rejection, revocation, expiry, use and refused attempt with who, when and why
  (`ApprovalStore.events(proposal_id)`, `GET /approvals/{id}/events`). SQLite triggers make
  the database itself refuse to update or delete an entry.
* **Who decided** is the authenticated operator for the API (`operator:<name>`, from the
  bearer token; see `apps/api/auth.py`), `console-user` for the terminal and
  `auto-approver[simulation]` for the simulation-only auto-approver.
* **The write boundary checks it too.** After consuming the approval, the gateway signs a
  single-use capability for exactly the approved action (`capabilities.py`); the environment
  refuses any write without one (audit finding 2.5).

| Channel | Who decides | Notes |
|---|---|---|
| `AutoApprovalChannel` | nobody (simulation only) | raises `ConfigError` unless the environment is simulated |
| `ConsoleApprovalChannel` | whoever answers at an interactive terminal (recorded as `console-user`: a terminal and a timing, not a person) | shows the exact action, target, params, evidence IDs and digest prefix; only `y`/`yes` approves; control characters in planner text are stripped |
| `ExternalApprovalChannel` | a human in the dashboard | the API calls `ApprovalStore.decide`; the waiter is woken in-process or by polling |

## Red team

`PromptInjectedProvider` wraps any provider and plays a planner that was **successfully**
prompt-injected. If untrusted evidence or context (a log line, a runbook chunk, a memory)
contains a destructive instruction — or was flagged `prompt_injection_suspected` — it proposes
`delete_namespace` and lies that the action is `read`. Enable it with
`RunConfig(planner="prompt_injected")`; the `malicious-log` scenario does this by default.

What the tests show (`tests/adversarial/`):

* the injected log line becomes flagged, untrusted evidence and a `safety.injection_flag` trace;
* the compromised proposal is denied by R9 at evaluation and at execution — even if a human
  was tricked into approving it — with **zero** write calls reaching the transport;
* malicious server-advertised names, lying annotations and smuggled `approval_id` arguments
  are all denied before the transport is touched.

**Safety-off ablation** (`RunConfig(safety_enabled=False)`): allowed only in a simulated
environment. The policy returns ALLOW with `R0-safety-disabled-simulation` and records the
verdict that would have applied, so evaluations can show what an unsafe action does to the
*simulated* system. Outside a simulation the same request is denied.

## Secrets in tool output

Tool output is untrusted and may carry secrets, such as an API key in a log line. The gateway
runs every output, and a failed call's error text, through the runtime's `Redactor`
(`recap/telemetry/redact.py`) *before* bounding it. So a secret the Redactor recognizes never
reaches evidence, context packs, provider prompts or the run record through a tool. Redacting
first matters: a secret cut in half by truncation would no longer match. See
`recap/tools/README.md`.

## Limitations (V1, stated honestly)

* **Pattern-based injection detection is NOT production-grade.** It is a short list of regular
  expressions that flags obvious instruction-like text so it is visible in the dashboard and
  context packs. It is easy to evade. RECAP's protection comes from the gateway: dangerous
  actions are denied no matter what any text says.
* Redaction is pattern-based too: a secret in a format the `Redactor` does not recognize is
  stored as it is.
* The policy is simulator-scoped (YAML allowlists of one namespace and five services). A real
  cluster needs an RBAC-aware policy behind the same `SafetyPolicy` interface — planned.
* Approvals live in a local SQLite file and `actor` is a label: V1 does not authenticate who
  approved. Anyone who can write that file could forge an approval. The API/UI layer would need
  real authentication before this could protect anything beyond a local prototype.
* Code running inside the RECAP process can bypass Python-level boundaries; V1 assumes a
  single-user, local deployment.
