import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  arr,
  fmtArgs,
  fmtMs,
  hypothesisTone,
  num,
  obj,
  riskTone,
  roleLabel,
  stateLabel,
  stateTone,
  str,
  tsMs,
  verdictTone,
} from "../format";
import type { Tone } from "../format";
import { useElementWidth } from "../hooks";
import type {
  AgentFinishPayload,
  AgentStartPayload,
  ApprovalDecidedPayload,
  EvidenceCheckPayload,
  InjectionPayload,
  MemoryWritePayload,
  MitigationExecutedPayload,
  OrchestrationAgent,
  OrchestrationPayload,
  PolicyPayload,
  ProposalPayload,
  RecoveryPayload,
  RoutingPayload,
  StatePayload,
  ToolCallPayload,
  ToolResultPayload,
  TraceEvent,
} from "../types";
import { Chip, FlagChip, Mono, VerdictChip } from "./Badge";
import { Segmented, Toggle } from "./Card";
import { DataTable } from "./Table";

// ======================================================================================
// Model: trace events -> bars, spans, markers, phases (all times in ms since the first event)
// ======================================================================================

const LANE_ORDER = ["controller", "investigator", "telemetry_investigator", "infrastructure_investigator"];

export interface ToolBar {
  id: string;
  lane: string;
  tool: string;
  start: number;
  end: number;
  status: "ok" | "failed" | "running";
  args: Record<string, unknown>;
  evidenceId: string | null;
  error: string | null;
  durationMs: number | null;
  flags: string[];
  /** Side effects of a write (mitigations only). */
  note: string | null;
  verdict: string | null;
  ruleIds: string[];
  callSeq: number;
  resultSeq: number;
}

export interface AgentSpan {
  lane: string;
  round: number;
  start: number;
  end: number;
  open: boolean;
}

export interface Marker {
  id: string;
  lane: string;
  t: number;
  tone: Tone;
  label: string;
  detail: string;
}

export interface Phase {
  state: string;
  start: number;
  end: number;
  reason: string;
}

export interface OverlapWindow {
  start: number;
  end: number;
  overlapMs: number | null;
  lanes: string[];
}

export interface TimelineModel {
  total: number;
  lanes: string[];
  bars: ToolBar[];
  spans: AgentSpan[];
  markers: Marker[];
  phases: Phase[];
  windows: OverlapWindow[];
}

function laneOf(agent: unknown): string {
  return str(agent) || "controller";
}

export function buildTimeline(events: TraceEvent[]): TimelineModel | null {
  if (!events.length) return null;
  const t0 = tsMs(events[0].ts);
  if (!Number.isFinite(t0)) return null;
  const rel = (iso: string | null | undefined): number | null => {
    const t = tsMs(iso);
    return Number.isFinite(t) ? Math.max(0, t - t0) : null;
  };

  let last = 0;
  const bars: ToolBar[] = [];
  const openBars = new Map<string, ToolBar>();
  const spans: AgentSpan[] = [];
  const openSpans = new Map<string, AgentSpan>();
  const markers: Marker[] = [];
  const transitions: { t: number; to: string; reason: string }[] = [];
  const windows: OverlapWindow[] = [];
  const policies: { seq: number; lane: string; tool: string; verdict: string; ruleIds: string[] }[] = [];

  const mark = (e: TraceEvent, lane: string, t: number, tone: Tone, label: string, detail = "") =>
    markers.push({ id: `m${e.seq}`, lane, t, tone, label, detail });

  for (const e of events) {
    const t = rel(e.ts);
    if (t === null) continue;
    last = Math.max(last, t);
    const p = e.payload ?? {};

    switch (e.category) {
      case "tool.call": {
        const q = p as ToolCallPayload;
        const bar: ToolBar = {
          id: str(q.invocation_id) || `seq-${e.seq}`,
          lane: laneOf(e.agent ?? q.caller),
          tool: str(q.tool, "?"),
          start: t,
          end: t,
          status: "running",
          args: obj(q.args),
          evidenceId: null,
          error: null,
          durationMs: null,
          flags: [],
          note: null,
          verdict: null,
          ruleIds: [],
          callSeq: e.seq,
          resultSeq: Number.POSITIVE_INFINITY,
        };
        bars.push(bar);
        openBars.set(bar.id, bar);
        break;
      }
      case "tool.result": {
        const q = p as ToolResultPayload;
        let bar = q.invocation_id ? openBars.get(q.invocation_id) : undefined;
        if (!bar) {
          // No invocation id: pair with the latest open call of the same tool (and agent).
          const candidates = [...openBars.values()].filter(
            (b) => b.tool === q.tool && (!e.agent || b.lane === e.agent),
          );
          bar = candidates[candidates.length - 1];
        }
        if (!bar) break;
        openBars.delete(bar.id);
        const duration = num(q.duration_ms) ?? num(e.duration_ms) ?? Math.max(0, t - bar.start);
        bar.durationMs = duration;
        bar.end = bar.start + duration;
        bar.status = q.ok === false ? "failed" : "ok";
        bar.evidenceId = q.evidence_id ?? null;
        bar.error = q.error ?? null;
        bar.flags = arr<string>(q.flags).slice();
        if (q.truncated && !bar.flags.includes("truncated")) bar.flags.push("truncated");
        bar.resultSeq = e.seq;
        last = Math.max(last, bar.end);
        break;
      }
      case "policy.decision": {
        const q = p as PolicyPayload;
        const lane = laneOf(q.caller ?? e.agent);
        const verdict = str(q.verdict, "?");
        policies.push({ seq: e.seq, lane, tool: str(q.tool), verdict, ruleIds: arr<string>(q.rule_ids) });
        // Mitigation verdicts and any denial are decisions worth a marker.
        if (lane === "controller" || verdict === "DENY") {
          mark(e, lane, t, verdictTone(verdict), `policy ${verdict} · ${str(q.tool, "?")}`, arr<string>(q.reasons).join("; "));
        }
        break;
      }
      case "agent.start": {
        const q = p as AgentStartPayload;
        const lane = laneOf(e.agent ?? q.role);
        const span: AgentSpan = { lane, round: num(q.round) ?? 1, start: t, end: t, open: true };
        spans.push(span);
        openSpans.set(`${lane}#${span.round}`, span);
        break;
      }
      case "agent.finish": {
        const q = p as AgentFinishPayload;
        const lane = laneOf(e.agent ?? q.role);
        const span =
          openSpans.get(`${lane}#${num(q.round) ?? 1}`) ?? [...openSpans.values()].find((s) => s.lane === lane);
        if (span) {
          span.end = t;
          span.open = false;
          openSpans.delete(`${span.lane}#${span.round}`);
        }
        break;
      }
      case "state.transition": {
        const q = p as StatePayload;
        transitions.push({ t, to: str(q.to, "?"), reason: str(q.reason) });
        break;
      }
      case "orchestration.summary": {
        const q = p as OrchestrationPayload;
        if (q.mode !== "parallel") break;
        const agents = arr<OrchestrationAgent>(q.agents);
        const starts = agents.map((a) => rel(a.started_at)).filter((x): x is number => x !== null);
        const ends = agents.map((a) => rel(a.finished_at)).filter((x): x is number => x !== null);
        if (starts.length >= 2 && ends.length >= 2) {
          const ws = Math.max(...starts);
          const we = Math.min(...ends);
          if (we > ws) {
            windows.push({ start: ws, end: we, overlapMs: num(q.overlap_ms), lanes: agents.map((a) => laneOf(a.role)) });
          }
        }
        break;
      }
      case "routing.decision": {
        const q = p as RoutingPayload;
        mark(e, "controller", t, "accent", `route round ${str(q.round, "?")}: ${str(q.mode, "?")}`, `${str(q.rule_id)} · ${str(q.reason)}`);
        break;
      }
      case "verification.evidence": {
        const q = p as EvidenceCheckPayload;
        if (q.proposal_check) {
          const ok = q.proposal_check.ok === true;
          mark(e, "controller", t, ok ? "ok" : "bad", `proposal check ${ok ? "passed" : "failed"}`, arr<string>(q.proposal_check.problems).join("; "));
        } else {
          mark(e, "controller", t, hypothesisTone(q.status), `evidence ${str(q.status, "?")}`, str(q.best?.claim) || str(q.summary));
        }
        break;
      }
      case "mitigation.proposed": {
        const q = p as ProposalPayload;
        if (q.proposal) {
          const risk = str(q.trusted_risk, "unknown");
          mark(e, "controller", t, riskTone(risk) === "bad" ? "bad" : "accent", `proposal: ${q.proposal.action} on ${q.proposal.target}`, `trusted risk ${risk} · source ${q.proposal.source}`);
        } else {
          mark(e, "controller", t, "bad", "no proposal drafted", arr<string>(q.notes).join("; "));
        }
        break;
      }
      case "approval.requested":
        mark(e, "controller", t, "warn", "approval requested", "waiting for an exact, single-use approval");
        break;
      case "approval.decided": {
        const q = p as ApprovalDecidedPayload;
        mark(e, "controller", t, q.decision === "approved" ? "ok" : "bad", `approval ${str(q.decision, "?")}`, q.actor ? `by ${q.actor}` : "");
        break;
      }
      case "mitigation.executed": {
        // The gateway's write path emits only this event (emitted when the call returns,
        // with its duration), so the write becomes a bar ending here on the controller lane.
        const q = p as MitigationExecutedPayload;
        const id = str(q.invocation_id) || `mit-${e.seq}`;
        const lane = laneOf(e.agent ?? "controller");
        const tool = str(q.tool, "?");
        // Already drawn from a tool.call/tool.result pair (same id, or same write just before)?
        if (bars.some((b) => b.id === id || (b.lane === lane && b.tool === tool && Math.abs(b.resultSeq - e.seq) <= 3))) break;
        const duration = num(e.duration_ms) ?? 0;
        bars.push({
          id,
          lane,
          tool,
          start: Math.max(0, t - duration),
          end: t,
          status: q.ok === false ? "failed" : "ok",
          args: { target: q.target, ...obj(q.params) },
          evidenceId: q.evidence_id ?? null,
          error: q.error ?? null,
          durationMs: duration,
          flags: [],
          note: arr<string>(q.side_effects).join("; ") || null,
          verdict: null,
          ruleIds: [],
          callSeq: e.seq - 1,
          resultSeq: e.seq,
        });
        break;
      }
      case "verification.recovery": {
        const q = p as RecoveryPayload;
        mark(e, "controller", t, q.verified ? "ok" : "bad", q.verified ? "oracle: recovered" : "oracle: not recovered", `${str(q.oracle)} · healthy before ${str(q.healthy_before)} → after ${str(q.healthy_after)}`);
        break;
      }
      case "memory.consolidated": {
        const q = p as MemoryWritePayload;
        mark(e, "controller", t, q.verified ? "ok" : "neutral", `episode written (${str(q.outcome, "?")})`, str(q.episode_id));
        break;
      }
      case "safety.injection_flag": {
        const q = p as InjectionPayload;
        mark(e, laneOf(e.agent), t, "warn", "injection-like text flagged", `${str(q.evidence_id)} · ${arr<string>(q.patterns).join(", ")}`);
        break;
      }
      case "budget.exhausted":
        mark(e, "controller", t, "warn", "budget exhausted", str((p as { limit?: unknown }).limit));
        break;
      case "run.error":
        mark(e, "controller", t, "bad", "run error", str((p as { error?: unknown }).error));
        break;
      default:
        break;
    }
  }

  // Anything still in flight ends at the last observed moment.
  for (const bar of openBars.values()) bar.end = Math.max(bar.start, last);
  for (const span of openSpans.values()) span.end = Math.max(span.start, last);

  // Attach the gateway verdict to each call: same lane and tool, closest to the call.
  for (const bar of bars) {
    const candidates = policies.filter(
      (pd) => pd.tool === bar.tool && pd.lane === bar.lane && pd.seq >= bar.callSeq - 2 && pd.seq <= bar.resultSeq,
    );
    candidates.sort((a, b) => Math.abs(a.seq - bar.callSeq) - Math.abs(b.seq - bar.callSeq));
    if (candidates[0]) {
      bar.verdict = candidates[0].verdict;
      bar.ruleIds = candidates[0].ruleIds;
    }
  }

  // Concurrency windows for running runs (no orchestration summary yet): overlapping spans per round.
  if (!windows.length) {
    const byRound = new Map<number, AgentSpan[]>();
    for (const span of spans) byRound.set(span.round, [...(byRound.get(span.round) ?? []), span]);
    for (const group of byRound.values()) {
      if (group.length < 2) continue;
      const ws = Math.max(...group.map((s) => s.start));
      const we = Math.min(...group.map((s) => s.end));
      if (we > ws) windows.push({ start: ws, end: we, overlapMs: null, lanes: group.map((s) => s.lane) });
    }
  }

  transitions.sort((a, b) => a.t - b.t);
  const phases: Phase[] = transitions.map((tr, i) => ({
    state: tr.to,
    start: tr.t,
    end: i + 1 < transitions.length ? transitions[i + 1].t : last,
    reason: tr.reason,
  }));

  const present = new Set<string>(["controller"]);
  for (const b of bars) present.add(b.lane);
  for (const s of spans) present.add(s.lane);
  for (const m of markers) present.add(m.lane);
  const lanes = [
    ...LANE_ORDER.filter((l) => present.has(l)),
    ...[...present].filter((l) => !LANE_ORDER.includes(l)).sort(),
  ];

  return { total: Math.max(last, 1), lanes, bars, spans, markers, phases, windows };
}

// ======================================================================================
// Scale: linear time, optionally with long approval waits compressed into a short break
// ======================================================================================

interface Scale {
  x: (t: number) => number;
  ticks: number[];
  step: number;
  breaks: { start: number; end: number; x0: number; x1: number }[];
}

function niceStep(raw: number): number {
  if (!(raw > 0)) return 1;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const m = raw / pow;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * pow;
}

function makeScale(total: number, waits: { start: number; end: number }[], width: number): Scale {
  const cut = [...waits].sort((a, b) => a.start - b.start);
  const waitSum = cut.reduce((s, w) => s + (w.end - w.start), 0);
  const rest = Math.max(total - waitSum, 1);
  const gap = cut.length ? rest * 0.08 : 0; // display length of one compressed wait
  const displayTotal = rest + gap * cut.length;

  // Piecewise-linear "display time": normal segments 1:1, each wait squeezed into `gap`.
  const display = (t: number): number => {
    let d = 0;
    let cursor = 0;
    for (const w of cut) {
      if (t <= w.start) break;
      d += w.start - cursor;
      if (t < w.end) return d + gap * ((t - w.start) / (w.end - w.start));
      d += gap;
      cursor = w.end;
    }
    return d + (t - cursor);
  };
  const x = (t: number) => (display(Math.min(Math.max(t, 0), total)) / displayTotal) * width;

  const step = niceStep(displayTotal / Math.max(2, Math.floor(width / 96)));
  const ticks: number[] = [];
  let t = 0;
  for (let guard = 0; t <= total + 1e-6 && guard < 600; guard++) {
    const inside = cut.find((w) => t > w.start && t < w.end);
    if (inside) {
      t = Math.ceil(inside.end / step) * step;
      continue;
    }
    if (!ticks.length || x(t) - x(ticks[ticks.length - 1]) >= 58) ticks.push(t);
    t = Math.round((t + step) * 1000) / 1000;
  }
  return { x, ticks, step, breaks: cut.map((w) => ({ ...w, x0: x(w.start), x1: x(w.end) })) };
}

function tickLabel(t: number, step: number): string {
  if (t === 0) return "0";
  if (step < 1000) return t < 1000 ? `${Math.round(t)} ms` : `${(t / 1000).toFixed(step < 100 ? 2 : 1)} s`;
  return `${(t / 1000).toFixed(step % 1000 === 0 ? 0 : 1)} s`;
}

// ======================================================================================
// Chart
// ======================================================================================

const GUTTER = 196; // lane labels
const PAD_R = 18;
const PHASE_H = 24;
const AXIS_H = 26;
const LANES_Y = PHASE_H + AXIS_H + 6;
const LANE_H = 50;
const BAR_H = 16;
const MONO_CHAR = 7.3; // approx. width of a 12px monospace glyph
const SANS_CHAR = 6.7;

interface Tip {
  x: number;
  y: number;
  content: ReactNode;
}

function BarTip({ bar }: { bar: ToolBar }) {
  return (
    <>
      <div className="tip-title mono">{bar.tool}</div>
      <div className="tip-row">
        <span>agent</span>
        <span className="mono">{bar.lane}</span>
      </div>
      <div className="tip-row">
        <span>args</span>
        <span className="mono">{fmtArgs(bar.args) || "—"}</span>
      </div>
      <div className="tip-row">
        <span>duration</span>
        <strong>{bar.status === "running" ? "in flight" : fmtMs(bar.durationMs)}</strong>
      </div>
      <div className="tip-row">
        <span>starts at</span>
        <span>+{fmtMs(bar.start)}</span>
      </div>
      <div className="tip-row">
        <span>evidence</span>
        <span className="mono">{bar.evidenceId ?? "—"}</span>
      </div>
      {bar.verdict ? (
        <div className="tip-row">
          <span>policy</span>
          <span>
            {bar.verdict}
            {bar.ruleIds.length ? <span className="mono"> · {bar.ruleIds[bar.ruleIds.length - 1]}</span> : null}
          </span>
        </div>
      ) : null}
      {bar.flags.length ? (
        <div className="tip-row">
          <span>flags</span>
          <span className="mono">{bar.flags.join(", ")}</span>
        </div>
      ) : null}
      {bar.note ? <div className="tip-note">{bar.note}</div> : null}
      {bar.error ? <div className="tip-error">{bar.error}</div> : null}
    </>
  );
}

export function AgentGantt({ model }: { model: TimelineModel }) {
  const [setWrap, width] = useElementWidth<HTMLDivElement>();
  const [view, setView] = useState<"chart" | "table">("chart");
  const [compress, setCompress] = useState(true);
  const [tip, setTip] = useState<Tip | null>(null);

  const waits = useMemo(
    () => model.phases.filter((p) => p.state === "AWAITING_APPROVAL" && p.end > p.start),
    [model.phases],
  );
  const waitTotal = waits.reduce((s, w) => s + (w.end - w.start), 0);
  // Compressing only matters when a human wait dominates the run.
  const canCompress = waitTotal > 1000 && waitTotal > 0.25 * model.total;
  const plotW = Math.max(240, width - GUTTER - PAD_R);
  const scale = useMemo(
    () => makeScale(model.total, canCompress && compress ? waits : [], plotW),
    [model.total, waits, canCompress, compress, plotW],
  );
  const X = (t: number) => GUTTER + scale.x(t);

  const lanesBottom = LANES_Y + model.lanes.length * LANE_H;
  const height = lanesBottom + (model.windows.length ? 34 : 10);
  const laneIndex = new Map(model.lanes.map((l, i) => [l, i]));
  const laneY = (lane: string) => LANES_Y + (laneIndex.get(lane) ?? 0) * LANE_H;
  const callCount = (lane: string) => model.bars.filter((b) => b.lane === lane).length;

  const show = (x: number, y: number, content: ReactNode) => setTip({ x, y, content });
  const hide = () => setTip(null);

  return (
    <div className="gantt">
      <div className="gantt-toolbar">
        <Segmented
          label="Timeline view"
          value={view}
          onChange={setView}
          options={[
            { value: "chart", label: "Chart" },
            { value: "table", label: "Table" },
          ]}
        />
        {canCompress ? (
          <Toggle
            checked={compress}
            onChange={setCompress}
            label="Compress approval wait"
            hint={`a human took ${fmtMs(waitTotal)} to decide`}
          />
        ) : null}
        <div className="gantt-legend" aria-label="Legend">
          <span><i className="lg lg-bar" /> tool call</span>
          <span><i className="lg lg-bar-bad" /> failed or denied</span>
          <span><i className="lg lg-span" /> agent active</span>
          <span><i className="lg lg-dot" /> controller decision</span>
          {model.windows.length ? <span><i className="lg lg-window" /> agents concurrent</span> : null}
        </div>
      </div>

      {view === "table" ? (
        <DataTable
          rows={[...model.bars].sort((a, b) => a.start - b.start)}
          rowKey={(b) => b.id}
          empty="No tool calls recorded for this run."
          columns={[
            { key: "lane", header: "Agent", render: (b) => <Mono>{b.lane}</Mono> },
            { key: "tool", header: "Tool call", render: (b) => <Mono>{`${b.tool}(${fmtArgs(b.args)})`}</Mono> },
            { key: "start", header: "Start", numeric: true, render: (b) => `+${fmtMs(b.start)}` },
            { key: "dur", header: "Duration", numeric: true, render: (b) => (b.status === "running" ? "in flight" : fmtMs(b.durationMs)) },
            {
              key: "status",
              header: "Result",
              render: (b) => (
                <Chip tone={b.status === "ok" ? "ok" : b.status === "failed" ? "bad" : "accent"}>
                  {b.status === "ok" ? "ok" : b.status === "failed" ? "failed" : "running"}
                </Chip>
              ),
            },
            { key: "policy", header: "Policy", render: (b) => (b.verdict ? <VerdictChip verdict={b.verdict} /> : "—") },
            { key: "ev", header: "Evidence", render: (b) => (b.evidenceId ? <Mono>{b.evidenceId}</Mono> : "—") },
            {
              key: "flags",
              header: "Flags",
              render: (b) => (b.flags.length ? <span className="chip-row">{b.flags.map((f) => <FlagChip key={f} flag={f} />)}</span> : "—"),
            },
          ]}
        />
      ) : (
        <div className="gantt-chart" ref={setWrap} onPointerLeave={hide}>
          {width > 0 ? (
            <svg width={width} height={height} role="group" aria-label={`Agent timeline: ${model.bars.length} tool calls across ${model.lanes.length} lanes over ${fmtMs(model.total)}`}>
              {/* lifecycle ribbon */}
              <text x={14} y={PHASE_H / 2 + 4} className="gantt-gutter-label">
                Lifecycle
              </text>
              {model.phases.map((ph, i) => {
                const x0 = X(ph.start);
                const w = Math.max(2, X(ph.end) - x0 - 2); // 2px surface gap between phases
                const label = stateLabel(ph.state);
                const fits = w > label.length * SANS_CHAR + 12;
                return (
                  <g
                    key={`${ph.state}-${i}`}
                    tabIndex={0}
                    className="gantt-hit"
                    onPointerEnter={() =>
                      show(x0 + w / 2, PHASE_H, <>
                        <div className="tip-title">{label}</div>
                        <div className="tip-row"><span>from</span><span>+{fmtMs(ph.start)}</span></div>
                        <div className="tip-row"><span>for</span><strong>{fmtMs(ph.end - ph.start)}</strong></div>
                        {ph.reason ? <div className="tip-note">{ph.reason}</div> : null}
                      </>)
                    }
                    onFocus={() => show(x0 + w / 2, PHASE_H, <div className="tip-title">{label} · {fmtMs(ph.end - ph.start)}</div>)}
                    onBlur={hide}
                    onPointerLeave={hide}
                  >
                    <rect x={x0} y={1} width={w} height={PHASE_H - 4} rx={4} className={`phase tone-${stateTone(ph.state)}`} />
                    {fits ? (
                      <text x={x0 + 6} y={PHASE_H / 2 + 3} className="phase-label">
                        {label}
                      </text>
                    ) : null}
                  </g>
                );
              })}

              {/* time axis and gridlines */}
              <text x={14} y={PHASE_H + 18} className="gantt-gutter-label">
                time since alert
              </text>
              {scale.ticks.map((t) => (
                <g key={t}>
                  <line x1={X(t)} x2={X(t)} y1={LANES_Y - 4} y2={lanesBottom} className="gridline" />
                  <text x={X(t)} y={PHASE_H + 18} className="tick-label" textAnchor={t === 0 ? "start" : "middle"}>
                    {tickLabel(t, scale.step)}
                  </text>
                </g>
              ))}

              {/* lanes */}
              {model.lanes.map((lane, i) => {
                const y = LANES_Y + i * LANE_H;
                const calls = callCount(lane);
                return (
                  <g key={lane}>
                    <rect x={0} y={y} width={width} height={LANE_H} className={i % 2 ? "lane lane-alt" : "lane"} />
                    <text x={14} y={y + 21} className="lane-label">
                      {roleLabel(lane)}
                    </text>
                    <text x={14} y={y + 38} className="lane-sub">
                      {lane === "controller" ? "trusted, the only writer" : `${calls} tool call${calls === 1 ? "" : "s"}`}
                    </text>
                  </g>
                );
              })}

              {/* concurrency windows: when parallel specialists were active at the same time */}
              {model.windows.map((w, i) => {
                const idx = w.lanes.map((l) => laneIndex.get(l)).filter((v): v is number => v !== undefined);
                const top = idx.length ? LANES_Y + Math.min(...idx) * LANE_H : LANES_Y;
                const bottom = idx.length ? LANES_Y + (Math.max(...idx) + 1) * LANE_H : lanesBottom;
                const x0 = X(w.start);
                const x1 = X(w.end);
                const label = `agents concurrent for ${fmtMs(w.overlapMs ?? w.end - w.start)}`;
                const labelW = label.length * SANS_CHAR;
                const lx = x1 - x0 > labelW ? (x0 + x1) / 2 : Math.min(x0 + labelW / 2, width - PAD_R - labelW / 2);
                return (
                  <g key={`w${i}`}>
                    <rect x={x0} y={top + 2} width={Math.max(2, x1 - x0)} height={bottom - top - 4} rx={6} className="overlap-window" />
                    <line x1={x0} x2={x1} y1={lanesBottom + 9} y2={lanesBottom + 9} className="bracket" />
                    <line x1={x0} x2={x0} y1={lanesBottom + 4} y2={lanesBottom + 14} className="bracket" />
                    <line x1={x1} x2={x1} y1={lanesBottom + 4} y2={lanesBottom + 14} className="bracket" />
                    <text x={lx} y={lanesBottom + 28} textAnchor="middle" className="bracket-label">
                      {label}
                    </text>
                  </g>
                );
              })}

              {/* compressed approval waits (axis breaks) */}
              {scale.breaks.map((b, i) => {
                const x0 = GUTTER + b.x0;
                const x1 = GUTTER + b.x1;
                const mid = (x0 + x1) / 2;
                return (
                  <g key={`b${i}`}>
                    <rect x={x0} y={LANES_Y - 4} width={Math.max(2, x1 - x0)} height={lanesBottom - LANES_Y + 4} className="break-band" />
                    <line x1={mid - 6} x2={mid - 1} y1={LANES_Y + 2} y2={LANES_Y - 8} className="break-mark" />
                    <line x1={mid + 1} x2={mid + 6} y1={LANES_Y + 2} y2={LANES_Y - 8} className="break-mark" />
                    <text x={mid} y={PHASE_H + 18} textAnchor="middle" className="tick-label strong">
                      wait {fmtMs(b.end - b.start)}
                    </text>
                  </g>
                );
              })}

              {/* agent activity spans */}
              {model.spans.map((s, i) => {
                const y = laneY(s.lane);
                const x0 = X(s.start);
                return (
                  <rect
                    key={`s${i}`}
                    x={x0}
                    y={y + 7}
                    width={Math.max(3, X(s.end) - x0)}
                    height={LANE_H - 14}
                    rx={7}
                    className={s.open ? "agent-span open" : "agent-span"}
                  />
                );
              })}

              {/* tool calls */}
              {model.bars.map((bar) => {
                const y = laneY(bar.lane) + (LANE_H - BAR_H) / 2;
                const x0 = X(bar.start);
                const w = Math.max(3, X(bar.end) - x0);
                const fits = w > bar.tool.length * MONO_CHAR + 10;
                const denied = bar.status === "failed" || bar.verdict === "DENY";
                const content = <BarTip bar={bar} />;
                return (
                  <g
                    key={bar.id}
                    className="gantt-hit"
                    tabIndex={0}
                    aria-label={`${bar.lane}: ${bar.tool}, ${fmtMs(bar.durationMs)}`}
                    onPointerEnter={() => show(x0 + w / 2, y, content)}
                    onFocus={() => show(x0 + w / 2, y, content)}
                    onBlur={hide}
                    onPointerLeave={hide}
                  >
                    {/* invisible hit area larger than the mark */}
                    <rect x={x0 - 3} y={y - 6} width={Math.max(w + 6, 14)} height={BAR_H + 12} className="hit" />
                    <rect
                      x={x0}
                      y={y}
                      width={w}
                      height={BAR_H}
                      rx={4}
                      className={denied ? "bar bar-bad" : bar.status === "running" ? "bar bar-running" : "bar"}
                    />
                    {fits ? (
                      <text x={x0 + 5} y={y + BAR_H / 2 + 4} className="bar-label">
                        {bar.tool}
                      </text>
                    ) : null}
                  </g>
                );
              })}

              {/* controller decisions, denials and injection flags */}
              {model.markers.map((m) => {
                const cx = X(m.t);
                const cy = laneY(m.lane) + LANE_H / 2;
                const content = (
                  <>
                    <div className="tip-title">{m.label}</div>
                    <div className="tip-row"><span>at</span><span>+{fmtMs(m.t)}</span></div>
                    {m.detail ? <div className="tip-note">{m.detail}</div> : null}
                  </>
                );
                return (
                  <g
                    key={m.id}
                    className="gantt-hit"
                    tabIndex={0}
                    aria-label={m.label}
                    onPointerEnter={() => show(cx, cy - 6, content)}
                    onFocus={() => show(cx, cy - 6, content)}
                    onBlur={hide}
                    onPointerLeave={hide}
                  >
                    <circle cx={cx} cy={cy} r={12} className="hit" />
                    <circle cx={cx} cy={cy} r={5} className={`marker tone-${m.tone}`} />
                  </g>
                );
              })}
            </svg>
          ) : (
            <div style={{ height }} />
          )}
          {tip ? (
            <div
              className={`gantt-tip${tip.x > width - 170 ? " right" : tip.x < 170 ? " left" : ""}`}
              style={{ left: tip.x, top: tip.y }}
              role="tooltip"
            >
              {tip.content}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
