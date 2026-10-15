# ADR 0004: Reach the environment through a real MCP server over stdio

**In one sentence:** agents never call the environment directly; they use ten real MCP tools
served by a separate subprocess for each run, and RECAP treats that server as untrusted.

## Status

Accepted.

## Context

**MCP** (Model Context Protocol) is an open standard for how an agent lists and calls
tools. Messages are JSON-RPC (small JSON request and response objects). With the **stdio**
transport, the client starts the server as a child process and the two talk over the
child's standard input and output.

RECAP's agents must reach the infrastructure only through tools. V1 wants that tool boundary
to be the same kind of boundary a real deployment would have, so that it can show how an
agent should treat a tool server: as something that can lie, hang or return hostile output.
A plain Python function call would not show any of that.

## Decision

- **A real MCP server.** [recap/tools/mcp_server.py](../../recap/tools/mcp_server.py) uses
  the official Python MCP SDK (`mcp>=2.3,<3` in [pyproject.toml](../../pyproject.toml)).
  `build_mcp_server` publishes one `TargetEnvironment` as ten tools: seven reads
  (`list_services`, `get_service_health`, `get_service_logs`, `get_service_metrics`,
  `get_recent_deployments`, `get_k8s_like_events`, `propose_rollback`) and three writes
  (`execute_rollback`, `restart_service`, `delete_namespace`). All three writes go through
  the environment's single write path, `execute_authorized`.
- **Typed results.** Each tool returns an observation model from
  [recap/core/observations.py](../../recap/core/observations.py), so the SDK publishes an
  output schema and sends structured content plus a JSON text copy.
- **A real client.** `McpToolClient` in [recap/tools/mcp_client.py](../../recap/tools/mcp_client.py)
  implements the `ToolTransport` interface. `McpToolClient.stdio` starts
  `python -m recap.tools.mcp_server --env-config <json> --run-id <id>` as a subprocess
  (`stdio_transport` in [recap/runtime.py](../../recap/runtime.py) does this for every run).
  It leaves `env` unset, so the SDK passes only a small allowlist of environment variables
  and no API keys reach the server.
- **One server per run.** The server builds its own environment from the same config and run
  ID as the controller, so the tools and the recovery oracle see the same simulated world.
- **The server enforces nothing.** Its own instructions say so. Risk tiers, approvals,
  budgets and argument rules live in the gateway
  ([ADR 0005](0005-central-safety-gateway-and-executor.md)). The `approval_id` and
  `proposal_digest` sent with a write are only for the environment's audit log.
- **Tests use real MCP too.** `McpToolClient.in_process` still speaks JSON-RPC with the
  initialize handshake, only over in-memory streams.
  [tests/integration/test_mcp_stdio.py](../../tests/integration/test_mcp_stdio.py) covers the
  real subprocess path.

## Consequences

### Good

- The boundary is real: the same gateway could talk to a remote MCP server by swapping the
  `ToolTransport`.
- Untrusted-server behavior is testable. The adversarial tests feed the gateway look-alike
  tool names, lying annotations, hostile descriptions and huge outputs
  ([tests/adversarial/test_malicious_tools.py](../../tests/adversarial/test_malicious_tools.py)).
- Environment code runs in another process, and failures such as timeouts or a dead server
  come back as error results (`RawToolResult(ok=False)`) instead of crashing the run.
- Secrets stay in the controller process:
  `tests/integration/test_mcp_stdio.py::test_stdio_client_starts_the_server_module_without_forwarding_our_environment`.

### Bad

- Starting the subprocess takes about 0.4 to 0.5 seconds per run, and that time is included
  in the evaluation latency numbers.
- Two processes must share state. The simulator does it with one SQLite file per run
  ([ADR 0002](0002-sqlite-storage.md)); a real adapter needs the real system as the shared
  source of truth.
- **MCP is not a security boundary.** Any local code that can reach the server could call a
  write tool directly, and the server does not check approvals. Signed approvals verified by
  the tool server are planned, not built
  ([threat model](../threat-model.md#future-hardening)).
- Trace context is not passed into the subprocess, so tool-server internals do not appear
  in RECAP's traces.
- RECAP depends on the SDK's version 2 API (pinned below version 3).

## Alternatives considered

- **Direct Python calls to the environment.** Simpler and faster, but there would be no
  protocol boundary, and nothing realistic to test about untrusted servers.
- **MCP over HTTP.** That needs ports, authentication and TLS, none of which a local
  prototype needs. It stays possible later as a new `ToolTransport`.
- **One long-lived server for all runs.** Faster to start, but runs would share one process
  and their state would be harder to keep apart.
- **A home-made JSON protocol.** It would not interoperate with other MCP clients or
  servers, and it would teach nothing about the real standard.
