import { useEffect, useMemo } from "react";
import { api } from "../api";
import { Chip, Mono } from "../components/Badge";
import { Card, KeyValues, Meter, Notice, PageHeader, StatTile } from "../components/Card";
import { EmptyState, ErrorState, Loading } from "../components/EmptyState";
import { RunScope } from "../components/RunPicker";
import { AgentGantt, buildTimeline } from "../components/Timeline";
import { arr, fmtInt, fmtMs, fmtScore, isTerminal, num, obj, roleLabel, str } from "../format";
import { usePolling, useRunSelection } from "../hooks";
import type { PageProps } from "../hooks";
import type { OrchestrationAgent, OrchestrationPayload, RoutingPayload, TraceEvent } from "../types";

/** What each routing signal means (documentation for the signals table). */
const SIGNAL_NOTES: Record<string, string> = {
  services_affected: "services named by the alert",
  symptom_categories: "kinds of symptoms observed",
  memory_hits: "similar past episodes found",
  top_memory_similarity: "best memory similarity (0–1)",
  familiar_success: "a verified success is very similar",
  memory_conflict: "similar episodes disagree",
  prior_failed_attempts: "failed mitigations in this run + very similar FAILED past episodes",
  escalation: "previous round was inconclusive",
  remaining_tool_calls: "tool-call budget left",
  remaining_steps: "step budget left",
  max_parallel_agents: "parallelism cap",
};

function signalValue(value: unknown) {
  if (typeof value === "boolean") return <Chip tone={value ? "accent" : "neutral"}>{value ? "yes" : "no"}</Chip>;
  if (typeof value === "number") return <span className="num">{Number.isInteger(value) ? fmtInt(value) : fmtScore(value)}</span>;
  if (Array.isArray(value)) return value.length ? <span className="chip-row">{value.map((v, i) => <Chip key={i}>{str(v)}</Chip>)}</span> : <span className="muted">none</span>;
  return <span>{str(value, "—")}</span>;
}

function OrchestrationView({ event }: { event: TraceEvent | undefined }) {
  if (!event) return <span className="muted">Agents still running…</span>;
  const p = event.payload as OrchestrationPayload;
  const agents = arr<OrchestrationAgent>(p.agents);
  const wall = num(p.wall_ms);
  const sum = num(p.sum_ms);
  const overlap = num(p.overlap_ms) ?? 0;
  const overlapped = p.mode === "parallel" && overlap > 0;
  const longest = Math.max(1, ...agents.map((a) => num(a.duration_ms) ?? 0));
  return (
    <div className="orch">
      <div className={overlapped ? "orch-headline ok" : "orch-headline"}>
        {p.mode !== "parallel"
          ? "Single agent — no concurrency in this round"
          : overlapped
            ? "Parallel round — the agents ran at the same time"
            : "Parallel round, but the agents did not overlap"}
      </div>
      {/* The overlap is measured. "Back to back" never ran (it is the sum of the agents' times), so it is labeled an estimate. */}
      <div className="muted">
        wall clock {fmtMs(wall)} ·{" "}
        {overlapped
          ? `agents overlapped for ${fmtMs(overlap)} (running them back to back would have taken about ${fmtMs(sum)} — an estimate)`
          : `sum of agent time ${fmtMs(sum)}`}
      </div>
      <div className="orch-agents">
        {agents.map((a, i) => (
          <div className="orch-agent" key={`${str(a.role)}-${i}`}>
            <Mono>{str(a.role, "agent")}</Mono>
            <Meter value={num(a.duration_ms) ?? 0} max={longest} label={fmtMs(num(a.duration_ms))} width={120} />
            {a.error ? <Chip tone="bad">{a.error}</Chip> : null}
          </div>
        ))}
      </div>
    </div>
  );
}

function RoutingRound({ event, orchestration }: { event: TraceEvent; orchestration: TraceEvent | undefined }) {
  const p = event.payload as RoutingPayload;
  const signals = obj(p.signals);
  const budget = obj(p.budget);
  return (
    <div className="round">
      <div className="round-head">
        <span className="round-title">Round {str(p.round, "?")}</span>
        <Chip tone="accent" mono>
          {str(p.mode, "?")}
        </Chip>
        <Mono>{str(p.rule_id, "?")}</Mono>
        <span className="chip-row">
          {arr<string>(p.agents).map((a) => (
            <Chip key={a} mono>
              {a}
            </Chip>
          ))}
        </span>
        {p.router ? <span className="muted">router {p.router}</span> : null}
      </div>
      {p.reason ? <p className="round-reason">{p.reason}</p> : null}
      <div className="round-grid">
        <div>
          <h3 className="mini-title">Signals the router saw</h3>
          <table className="table compact">
            <tbody>
              {Object.entries(signals).map(([key, value]) => (
                <tr key={key}>
                  <td>
                    <Mono>{key}</Mono>
                    {SIGNAL_NOTES[key] ? <div className="muted small">{SIGNAL_NOTES[key]}</div> : null}
                  </td>
                  <td className="num">{signalValue(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="round-side">
          <h3 className="mini-title">Step allocation</h3>
          {Object.keys(budget).length ? (
            <KeyValues rows={Object.entries(budget).map(([k, v]) => [<Mono>{k}</Mono>, `${str(v)} steps`])} />
          ) : (
            <span className="muted">—</span>
          )}
          <h3 className="mini-title">Orchestration</h3>
          <OrchestrationView event={orchestration} />
        </div>
      </div>
    </div>
  );
}

function TimelineView({ runId, running }: { runId: string; running: boolean }) {
  const events = usePolling(
    () => api.events(runId),
    (list) => (list?.some((e) => e.category === "run.summary") || !running ? null : 1500),
    [runId],
  );
  // A run that just started may appear in /runs a moment later: restart polling then.
  const { refresh } = events;
  useEffect(() => {
    if (running) refresh();
  }, [running, refresh]);
  const list = useMemo(() => events.data ?? [], [events.data]);
  const model = useMemo(() => buildTimeline(list), [list]);
  const routing = list.filter((e) => e.category === "routing.decision");
  const orchestration = list.filter((e) => e.category === "orchestration.summary");

  if (events.error && !events.data) return <ErrorState error={events.error} />;
  if (!events.data) return <Loading label="Loading trace events…" />;
  if (!model) return <EmptyState title="This run has no trace events yet" icon="timeline" />;

  const agentLanes = model.lanes.filter((l) => l !== "controller");
  const parallelRounds = routing.filter((e) => e.payload.mode === "parallel").length;
  const bestOverlap = Math.max(0, ...orchestration.map((e) => num(e.payload.overlap_ms) ?? 0));
  const approvalWait = model.phases
    .filter((p) => p.state === "AWAITING_APPROVAL")
    .reduce((sum, p) => sum + (p.end - p.start), 0);

  return (
    <>
      <div className="stat-grid">
        <StatTile
          label="Run duration"
          value={fmtMs(model.total)}
          note={approvalWait > 0 ? `includes ${fmtMs(approvalWait)} waiting for approval` : "first to last trace event"}
        />
        <StatTile label="Tool calls" value={fmtInt(model.bars.length)} note={`${model.bars.filter((b) => b.status === "failed").length} failed`} />
        <StatTile label="Agents" value={fmtInt(agentLanes.length)} note={agentLanes.map(roleLabel).join(", ") || "none"} />
        <StatTile label="Parallel rounds" value={fmtInt(parallelRounds)} tone={parallelRounds ? "accent" : "neutral"} />
        <StatTile
          label="Concurrent agent time"
          value={bestOverlap > 0 ? fmtMs(bestOverlap) : "—"}
          tone={bestOverlap > 0 ? "ok" : "neutral"}
          note="overlap_ms from orchestration.summary"
        />
      </div>

      <Card
        title="Agent timeline"
        subtitle="x = time since the incident was created · one lane per agent · bars are MCP tool calls (tool.call paired with tool.result by invocation_id)"
      >
        {model.bars.length === 0 && model.spans.length === 0 ? (
          <Notice tone="info" title="No agent activity recorded yet">
            Tool calls appear here as soon as the investigators start.
          </Notice>
        ) : null}
        <AgentGantt model={model} />
      </Card>

      <Card title="Routing decisions" subtitle="routing.decision events: the rule that fired, why, and the signals it used">
        {routing.length === 0 ? (
          <span className="muted">No routing decision recorded yet.</span>
        ) : (
          routing.map((event, i) => <RoutingRound key={event.seq} event={event} orchestration={orchestration[i]} />)
        )}
      </Card>
    </>
  );
}

export function AgentTimelinePage({ route }: PageProps) {
  const selection = useRunSelection(route);
  return (
    <>
      <PageHeader
        title="Agent Timeline"
        description="When each agent ran and which tools it called. In a parallel round the specialists' bars overlap in time: that overlap is measured, not drawn for effect."
      />
      <RunScope selection={selection} page="timeline">
        {(runId, summary) => <TimelineView runId={runId} running={!!summary && !isTerminal(summary.state)} />}
      </RunScope>
    </>
  );
}
