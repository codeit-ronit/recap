# ADR 0014: The write boundary verifies signed, single-use capabilities

**In one sentence:** every write carries an Ed25519-signed capability for exactly that
action, issued by the gateway after the policy allowed it and the approval was consumed, and
the environment adapter refuses any write whose capability is missing, forged, altered,
expired, reused or for another action, so the gateway's decision holds even when something
else calls the MCP server.

## Status

Accepted (M1, 2026-10-15). Fixes audit finding 2.5. Amends
[ADR 0004](0004-real-mcp-over-stdio.md) ("the server enforces nothing") and extends
[ADR 0005](0005-central-safety-gateway-and-executor.md).

## Context

In V1 the gateway was the only check. The MCP server's write tools accepted an empty or
made-up `approval_id` and the simulator applied the action. So anything that could speak MCP
to a server, for example a second `python -m recap.tools.mcp_server` started for an existing
run, could roll back, restart or delete the namespace. A finished run's approval ID (shown
in the dashboard) could unlock a different write, and a used approval could be replayed.

## Decision

- [recap/safety/capabilities.py](../../recap/safety/capabilities.py): `CapabilityIssuer`
  makes a fresh Ed25519 key pair per run; the private key lives only in the controller's
  memory. `issue(...)` signs a payload naming run, incident, action, target, params,
  approval ID, proposal digest, a random nonce and a 60 s expiry.
  `verify_capability(...)` checks the signature first, then every field against the action
  actually requested, the expiry, the nonce against those already used, and that a
  safety-off capability is accepted only by a simulation.
- `TargetEnvironment.bind_executor(public_key)` (new, abstract): the controller calls it
  right after `reset`. The simulator stores the public key in the run's state file, so the
  MCP server subprocess verifies against the same key without holding any secret.
- The simulator verifies **inside the same SQLite transaction** as the change and records
  the nonce there. A refused write is audited and changes nothing.
- [recap/tools/actions.py](../../recap/tools/actions.py): one mapping from write tools to
  environment actions, used by both the gateway (to sign) and the server (to build the
  action), so a valid capability matches exactly.
- The MCP write tools take a `capability` argument; `StrictToolServer` rejects any argument
  a tool does not define (the SDK's default drops them silently).
- The capability is never traced, stored in evidence, the run record or the sandbox audit
  log.

## Consequences

### Good

- All 19 AUDIT-2.5 reproductions pass (unsigned, forged, second server process, stolen
  approval, replay, smuggled arguments). The approved path and the simulation-only
  safety-off ablation still work.
- A process that can read the public key, or start another server, still cannot sign.

### Bad

- Every adapter must implement both halves (bind and verify). A real adapter must refuse
  safety-off capabilities and keep the key where all its processes can read it.
- The state file is trusted: whoever can rewrite it can bind a key of their own (the same
  trust that ADR 0002 already gives to RECAP's files).
- A capability proves "RECAP's gateway allowed this exact action", not that the approval was
  wise, and not who the human was (that is ADR 0015).

## Alternatives considered

- **Look the approval up in the server.** The gateway consumes the approval before the call,
  so the server would have to accept consumed approvals, and it would need access to the
  approval database.
- **A shared HMAC secret.** The server process would hold the secret, so a second server
  started with the same command line could sign too.
- **A separate privileged executor process.** Stronger isolation, but more moving parts;
  the capability check is the piece that process would need anyway.
