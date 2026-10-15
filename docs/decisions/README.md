# Architecture decision records

An **architecture decision record (ADR)** is a short note about one important design choice:
the situation that forced it (*context*), what was chosen (*decision*), what it costs and
what it gains (*consequences*), and what else was considered (*alternatives*). Together,
these notes explain why RECAP V1 looks the way it does.

| ADR | Decision | Status | In one line |
|---|---|---|---|
| [0001](0001-modular-monolith.md) | Build V1 as a modular monolith | accepted | One Python package with typed contracts, interfaces and one composition root; the MCP tool server is the only extra process. |
| [0002](0002-sqlite-storage.md) | Store everything in local SQLite files | accepted | Runs, traces, approvals, memory and the simulated world live under `data/`; single-use approvals are atomic SQL updates. |
| [0003](0003-deterministic-default-provider.md) | A scripted, deterministic policy is the default provider | accepted | The default "brain" is not an LLM, costs $0 and is reproducible; the Claude adapter is opt-in with your own key. |
| [0004](0004-real-mcp-over-stdio.md) | Reach the environment through a real MCP server over stdio | accepted | Ten real MCP tools in a subprocess per run; the server is treated as untrusted and enforces nothing. |
| [0005](0005-central-safety-gateway-and-executor.md) | One central safety gateway, and the controller as the only executor | accepted | One gateway judges every call from trusted config; reversible fixes need an exact single-use approval; destructive actions are always denied. |
| [0006](0006-simulated-environment-for-v1.md) | Use a deterministic simulated environment for V1 | accepted | A five-service shop that runs offline in seconds, can be broken safely, and is labeled "simulated" everywhere. |
| [0007](0007-replaceable-environment-adapter.md) | Make the target environment a replaceable adapter | accepted | Infrastructure sits behind `TargetEnvironment`; SREGym or Kubernetes can replace the simulator, and a test proves the swap's wiring. |

## How the decisions fit together

- **0001** sets the overall shape: modules behind interfaces, wired in one place.
- **0002, 0003 and 0006** keep V1 local, free and reproducible: SQLite files, a scripted
  provider and a simulated target system.
- **0004 and 0005** define the tool boundary and who may act: agents use MCP tools, every
  call passes the gateway, and only the controller executes a fix.
- **0007** keeps the target system swappable, so the work above carries over to real
  infrastructure.

The [threat model](../threat-model.md) builds on 0004, 0005 and 0007. It lists every threat
V1 defends against, where the defense lives in the code, and the test that proves it.

## Two kinds of status

- **ADR status** says whether a decision is current: `proposed`, `accepted`, `superseded by
  ADR NNNN` or `deprecated`. All seven ADRs above are `accepted`.
- **Feature status**, used across the documentation, says how real a part is:
  *implemented* (works in V1 and is tested), *simulated* (works only against the simulator,
  or stands in for something real), *adapter only* (an interface or optional bridge exists,
  nothing real is connected) and *not yet built* (planned only).

## Adding an ADR

1. Copy the template below into `NNNN-short-title.md`, using the next free number.
2. Keep it short and specific. Link the code it is about with relative links.
3. Never rewrite an accepted decision. When a decision changes, write a new ADR and mark the
   old one `superseded by ADR NNNN`.
4. Add a row to the table above.

```markdown
# ADR NNNN: <decision as a short sentence>

**In one sentence:** <the decision and its main reason>

## Status

Proposed.

## Context

<the situation and the forces that make a decision necessary>

## Decision

<what was chosen, with links to the code that implements it>

## Consequences

### Good

- <what gets easier or safer>

### Bad

- <what gets harder, riskier or more expensive>

## Alternatives considered

- **<option>.** <why it was not chosen>
```
