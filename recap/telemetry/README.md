# `recap/telemetry` — traces, live stream, redaction and cost labels

Every important step of a run becomes one structured `TraceEvent` (`recap/core/models.py`):
state transitions, routing decisions, retrieval, context packs, tool calls, policy
decisions, approvals, mitigations, verification and the final summary. The trace is the
run's audit log. The dashboard timeline, the API and the evaluation reports all read it.

**What V1 is, plainly:** V1 = local JSONL + SQLite event logs and a live in-process stream.
OpenTelemetry is an optional console bridge, not distributed tracing. There is no
collector and no hosted service, and nothing leaves the machine.

| File | What it does |
|---|---|
| `tracer.py` | `Tracer.emit(...)` numbers events per run (`seq`), redacts payloads and writes to every sink. A failing sink is logged and never breaks a run. `TraceCategory` lists every category. |
| `sinks.py` | `JsonlTraceSink` (one file per run), `SqliteTraceSink` (the `events` table the API reads), `BroadcastHub` + `BroadcastSink` (live stream for server-sent events). |
| `redact.py` | `Redactor`: removes secrets from trace payloads, tool outputs (before they become evidence) and run error text. |
| `usage.py` | `cost_label(...)`: one honest line about what a run cost. |
| `otel.py` | `OTelTraceSink` and `maybe_otel_sink(enabled)`: the optional OpenTelemetry console bridge. |

## Data flow

```
any component ──► tracer.emit(category, payload) ──► Redactor ──► TraceEvent(seq = n)
                                                                    │
      ┌─────────────────────────────┬───────────────────────────────┼──────────────────────────┐
      ▼                             ▼                               ▼                          ▼
SqliteTraceSink               JsonlTraceSink                 BroadcastSink              OTelTraceSink
data/recap.db (events)        data/traces/<run_id>.jsonl     └► BroadcastHub            (optional, stderr)
└► GET /runs/{id}/events                                        └► GET /runs/{id}/stream (SSE)
```

The sinks for a run are chosen in one place: `Runtime.trace_sinks` in `recap/runtime.py`.

## One event

One line copied verbatim from the trace file of a `bad-deployment` demo run
(`<data dir>/traces/run-629d86e680.jsonl`). It records the investigator asking for checkout's
logs. The gateway builds the payload from the call's `ToolRequest`:

```json
{"event_id":"evt-95620d5bae","run_id":"run-629d86e680","seq":18,"stage":"INVESTIGATING","ts":"2026-10-14T22:12:20.967082Z","agent":"investigator","category":"tool.call","payload":{"invocation_id":"call-92ae17a6ef","tool":"get_service_logs","caller":"investigator","args":{"service":"checkout"},"incident_id":"inc-86a58860eb","requested_at":"2026-10-14T22:12:20.967073Z"},"duration_ms":null,"usage":null,"schema_version":"recap.v1"}
```

**Illustrative only** (made up for this README, not copied from a run): if that payload had
carried a credential, the trace would store the placeholder instead of the value.

```text
emitted:  {"tool": "get_service_logs", "args": {"service": "checkout", "api_key": "sk-ant-..."}}
stored:   {"tool": "get_service_logs", "args": {"service": "checkout", "api_key": "[REDACTED]"}}
```

## Live streaming

`BroadcastHub` is an in-process publish/subscribe keyed by run ID, running on one asyncio
event loop. A client gets the full trace without gaps like this:

1. `queue = hub.subscribe(run_id)`, *before* reading history, so nothing is missed;
2. replay history with `SqliteTraceSink.list_events(run_id, after_seq)`;
3. follow the queue and skip events whose `seq` was already sent;
4. `hub.unsubscribe(run_id, queue)` when done.

Each subscriber queue holds 1000 events. If a client stalls and its queue fills up, new
live events are dropped for that client instead of slowing the run down. It can re-sync
from SQLite with `after_seq`.

## Redaction

`recap/runtime.py` builds one `Redactor`, and it is applied at three points:

- **Trace payloads:** `Tracer.emit` redacts every payload before any sink sees the event.
- **Tool outputs:** the tool gateway redacts each tool result before it becomes evidence, so
  the checkpoint fields built from evidence inherit the redaction. A failed call's error text
  is redacted too.
- **Run error text:** the controller redacts a failed run's error message before saving it.

The `Redactor` returns a redacted deep copy (the input is never changed) and applies four rules:

1. **Sensitive keys.** A value stored under a key like `api_key`, `token`, `secret`,
   `password`, `authorization` or `credential` becomes `[REDACTED]`. Numbers, booleans and
   `None` stay visible, because under such keys they are counts such as `tokens_before`,
   not credentials.
2. **Known secrets.** Values of environment variables whose name contains `KEY`, `SECRET`,
   `TOKEN` or `PASSWORD` (8+ characters) are replaced wherever they appear in text.
3. **Key formats.** `sk-ant-...`, `sk-...`, `AKIA...`, `ghp_...` and `Bearer ...` are replaced.
4. **Key and value in free text.** `password`, `passwd`, `pwd`, `secret`, `token` or `api_key`
   followed by `=` or `:` keeps the key and loses the value, so a log line such as
   `password=hunter2` is stored as `password=[REDACTED]`.

The pattern list is best-effort, not a guarantee: a secret in a format these rules do not
recognize is stored as it is.

## Cost labels

`cost_label(usage, provider_kind)` returns one of three lines:

| Provider | Usage | Label |
|---|---|---|
| scripted (default, not an LLM) | always local estimates | `$0.00 external API charged - token counts are estimates (deterministic mode)` |
| LLM | every call reported its token counts (`is_estimate=False` and `unreported_calls=0`) | `~$0.0123 estimated from provider-reported token counts` |
| LLM | still an estimate, or at least one call had no usage data (`is_estimate=True` or `unreported_calls > 0`) | `LLM provider: cost not fully reported (some calls had no usage data); tokens shown are estimates` |

Only the scripted policy gets the deterministic wording. The LLM dollar figure is computed from
the prices in `configs/providers.yaml`, so it is an estimate, not an invoice. When the usage is
itself an estimate, or a call returned no usage data, no dollar figure is shown, because one
computed from guessed tokens would read like a real bill.

## Optional OpenTelemetry bridge

```bash
pip install -e '.[otel]'
RECAP_ENABLE_OTEL=1 python scripts/demo.py --scenario bad-deployment --approve auto
```

Each event becomes one span: the span name is the event category, the attributes are
`run_id`, `stage`, `agent`, `seq` and `duration_ms`, and a timed event's span covers its
measured duration. The SDK's `ConsoleSpanExporter` prints the spans to stderr. The bridge
uses a private `TracerProvider`, so it never changes the OpenTelemetry settings of a host
application. If the flag is on but the package is missing, startup fails with
`OpenTelemetry is optional: pip install -e '.[otel]'`.

## Honest limits and upgrade path

- Spans are independent: no parent/child hierarchy, and no context is propagated into the
  MCP server subprocess. V2: an OTLP exporter to a local collector (for example Jaeger) with
  nested spans per agent and tool call.
- The live stream is in-process. Several API workers would need a shared broker.
- Redaction is pattern-based and can miss unusual secret formats, and it runs only at the three
  points listed above.
