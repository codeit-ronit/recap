import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  arr,
  fmtArgs,
  fmtInt,
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
import { isRunbookSearch } from "../view/findings";
import type { TextAuthor } from "../view/findings";
import { laneKey, laneName } from "../view/timeline";
import type { RequestSplit, RoundGeometry } from "../view/timeline";
import type {
  AgentFinishPayload,
  AgentStartPayload,
  ApprovalDecidedPayload,
  EvidenceCheckPayload,
  InjectionPayload,
  LlmCallPayload,
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

/** The fields of a trace event the chart reads (a TraceEvent, or a timeline entry turned into one). */
export type TraceLike = Pick<TraceEvent, "seq" | "ts" | "stage" | "agent" | "category" | "payload" | "duration_ms">;

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
  /** search_runbooks: answered by RECAP's own runbook index, not the environment's MCP server. */
  local: boolean;
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

/**
 * One model request (an `llm.call` event), placed where the trace recorded it: when its answer
 * had arrived. The trace has no start time for a request, so the chart draws a mark, never an
 * inferred bar; its duration is in the tooltip (for a replay: the original run's).
 */
export interface RequestMark {
  id: string;
  lane: string;
  t: number;
  step: number | null;
  purpose: string;
  model: string;
  attempt: number;
  ok: boolean;
  replayed: boolean;
  durationMs: number | null;
  serverMs: number | null;
  computeMs: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
}

/** An investigation round on the time axis: from its routing decision to its last event. */
export interface RoundBand {
  round: number;
  mode: string;
  start: number;
  end: number;
}

export interface TimelineModel {
  total: number;
  lanes: string[];
  bars: ToolBar[];
  spans: AgentSpan[];
  markers: Marker[];
  phases: Phase[];
  windows: OverlapWindow[];
  requests: RequestMark[];
  rounds: RoundBand[];
}

function laneOf(agent: unknown): string {
  return str(agent) || "controller";
}

export function buildTimeline(events: readonly TraceLike[]): TimelineModel | null {
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
  const requests: RequestMark[] = [];
  const rounds: RoundBand[] = [];
  let band: RoundBand | null = null;

  const mark = (e: TraceLike, lane: string, t: number, tone: Tone, label: string, detail = "") =>
    markers.push({ id: `m${e.seq}`, lane, t, tone, label, detail });

  for (const e of events) {
    const t = rel(e.ts);
    if (t === null) continue;
    last = Math.max(last, t);
    const p = e.payload ?? {};
    // A round lasts from its routing decision while the run is investigating.
    if (band && e.stage === "INVESTIGATING" && e.category !== "state.transition" && e.category !== "routing.decision") band.end = Math.max(band.end, t);

    switch (e.category) {
      case "tool.call": {
        const q = p as ToolCallPayload;
        const tool = str(q.tool, "?");
        const bar: ToolBar = {
          id: str(q.invocation_id) || `seq-${e.seq}`,
          lane: laneOf(e.agent ?? q.caller),
          tool,
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
          local: isRunbookSearch(tool),
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
          const candidates = [...openBars.values()].filter((b) => b.tool === q.tool && (!e.agent || b.lane === e.agent));
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
        if (band && e.stage === "INVESTIGATING") band.end = Math.max(band.end, bar.end);
        break;
      }
      case "llm.call": {
        const q = p as LlmCallPayload & { server_ms?: number | null; server_compute_ms?: number | null };
        requests.push({
          id: `q${e.seq}`,
          lane: laneOf(e.agent),
          t,
          step: num(q.step),
          purpose: str(q.purpose),
          model: str(q.model),
          attempt: num(q.attempt) ?? 1,
          ok: q.ok !== false,
          replayed: q.replayed === true,
          durationMs: num(q.duration_ms),
          serverMs: num(q.server_ms),
          computeMs: num(q.server_compute_ms),
          inputTokens: num(q.input_tokens),
          outputTokens: num(q.output_tokens),
        });
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
        const span = openSpans.get(`${lane}#${num(q.round) ?? 1}`) ?? [...openSpans.values()].find((s) => s.lane === lane);
        if (span) {
          span.end = t;
          span.open = false;
          openSpans.delete(`${span.lane}#${span.round}`);
        }
        break;
      }
      case "state.transition": {
        const q = p as StatePayload;
        const to = str(q.to, "?");
        transitions.push({ t, to, reason: str(q.reason) });
        if (to !== "INVESTIGATING") band = null;
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
        band = { round: num(q.round) ?? rounds.length + 1, mode: str(q.mode, "?"), start: t, end: t };
        rounds.push(band);
        break;
      }
      case "verification.evidence": {
        const q = p as EvidenceCheckPayload;
        if (q.proposal_check) {
          const ok = q.proposal_check.ok === true;
          mark(e, "controller", t, ok ? "ok" : "bad", `proposal check ${ok ? "passed" : "failed"}`, arr<string>(q.proposal_check.problems).join("; "));
        } else {
          // RECAP's checked fields and its own summary: the claim sentence is the agent's text.
          const cause = q.best?.mechanism ? `${str(q.best.mechanism)}@${str(q.best.target_service, "?")}` : "";
          mark(e, "controller", t, hypothesisTone(q.status), `evidence ${str(q.status, "?")}`, [cause, str(q.summary)].filter(Boolean).join(" · "));
        }
        break;
      }
      case "mitigation.proposed": {
        const q = p as ProposalPayload;
        if (q.proposal) {
          const risk = str(q.trusted_risk, "unknown");
          mark(
            e,
            "controller",
            t,
            riskTone(risk) === "bad" ? "bad" : "accent",
            `proposal: ${str(q.proposal.action, "?")} on ${str(q.proposal.target, "?")}`,
            `trusted risk ${risk} · source ${str(q.proposal.source, "?")}`,
          );
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
        // The gateway's write path emits mitigation.started before sending and this event
        // when the call returns (with its duration); the bar is drawn from this one only, so
        // the write becomes one bar ending here on the controller lane.
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
          local: false,
        });
        break;
      }
      case "verification.recovery": {
        const q = p as RecoveryPayload;
        mark(
          e,
          "controller",
          t,
          q.verified ? "ok" : "bad",
          q.verified ? "oracle: recovered" : "oracle: not recovered",
          `${str(q.oracle)} · healthy before ${str(q.healthy_before)} → after ${str(q.healthy_after)}`,
        );
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
        // The error text can quote a provider or a tool: shown as recorded, labeled as such.
        mark(e, "controller", t, "bad", "run error", `As recorded (may quote a provider or a tool): ${str((p as { error?: unknown }).error, "no text")}`);
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
  for (const q of requests) present.add(q.lane);
  const lanes = [...LANE_ORDER.filter((l) => present.has(l)), ...[...present].filter((l) => !LANE_ORDER.includes(l)).sort()];

  return { total: Math.max(last, 1), lanes, bars, spans, markers, phases, windows, requests, rounds };
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

const WIDE_GUTTER = 196; // lane labels
const NARROW_GUTTER = 108; // phones: short lane names
const PAD_R = 18;
const PHASE_H = 24;
const AXIS_H = 26;
const ROUNDS_H = 20;
const LANE_H = 50;
const BAR_H = 16;
const MONO_CHAR = 7.3; // approx. width of a 12px monospace glyph
const SANS_CHAR = 6.7;

interface Tip {
  x: number;
  y: number;
  content: ReactNode;
}

function BarTip({ bar, author }: { bar: ToolBar; author: TextAuthor | undefined }) {
  return (
    <>
      <div className="tip-title mono">{bar.tool}</div>
      <div className="tip-row">
        <span>agent</span>
        <span className="mono">{bar.lane}</span>
      </div>
      {bar.local ? (
        <>
          <div className="tip-row">
            <span>answered by</span>
            <span>RECAP's runbook index</span>
          </div>
          {/* The query is free text an agent wrote: shown as data, labeled untrusted. */}
          <div className="tip-row">
            <span>query (untrusted)</span>
            <span className="mono">{str(bar.args.query, "—")}</span>
          </div>
          <div className="tip-note">A local read tool, not the environment's MCP server. The query was written by {author?.label ?? "the agent"}.</div>
        </>
      ) : (
        <div className="tip-row">
          <span>args</span>
          <span className="mono">{fmtArgs(bar.args) || "—"}</span>
        </div>
      )}
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

/** "2,134 in / 72 out tokens", or "not reported" when the server sent no counts. */
function tokenText(input: number | null, output: number | null): string {
  if (input === null && output === null) return "not reported by the server";
  return `${fmtInt(input)} in / ${fmtInt(output)} out`;
}

function RequestTip({ mark }: { mark: RequestMark }) {
  const wait = mark.serverMs !== null && mark.computeMs !== null ? Math.max(0, mark.serverMs - mark.computeMs) : null;
  return (
    <>
      <div className="tip-title">
        Model answer · {laneName(mark.lane === "controller" ? null : mark.lane)}
        {mark.attempt > 1 ? ` · attempt ${mark.attempt}` : ""}
      </div>
      <div className="tip-row">
        <span>purpose</span>
        <span>
          {mark.purpose || "—"}
          {mark.step !== null ? ` · step ${mark.step}` : ""}
        </span>
      </div>
      <div className="tip-row">
        <span>{mark.replayed ? "took (original run)" : "took"}</span>
        <strong>{fmtMs(mark.durationMs)}</strong>
      </div>
      <div className="tip-row">
        <span>server time</span>
        <span>{mark.serverMs === null ? "not reported" : fmtMs(mark.serverMs)}</span>
      </div>
      {wait !== null ? (
        <div className="tip-row">
          <span>waited inside the server</span>
          <span>{fmtMs(wait)}</span>
        </div>
      ) : null}
      <div className="tip-row">
        <span>tokens</span>
        <span>{tokenText(mark.inputTokens, mark.outputTokens)}</span>
      </div>
      {!mark.ok ? <div className="tip-error">No usable answer: RECAP asked again or stopped the agent.</div> : null}
      <div className="tip-note">
        {mark.replayed
          ? "Answered from a recording: no model was called now. Its duration, server time and tokens are the original run's."
          : "Drawn when the answer was recorded. The trace has no start time for a request, so no bar is drawn for it."}
      </div>
    </>
  );
}

/**
 * The whole run on one time axis: one lane per agent plus the controller, the lifecycle phases,
 * each round as a band, tool calls as bars, model answers as diamonds, controller decisions as
 * dots, and the measured window where parallel agents ran.
 *
 * `author`: who wrote the agents' free text (runbook search queries), for the tooltip's label.
 * `replay`: the run replayed a recording, so the axis is this replay's own time while the model
 * requests' durations (tooltips) are the original run's.
 */
export function AgentGantt({ model, author, replay = false }: { model: TimelineModel; author?: TextAuthor; replay?: boolean }) {
  const [setWrap, width] = useElementWidth<HTMLDivElement>();
  const [view, setView] = useState<"chart" | "table">("chart");
  const [compress, setCompress] = useState(true);
  const [tip, setTip] = useState<Tip | null>(null);

  const waits = useMemo(() => model.phases.filter((p) => p.state === "AWAITING_APPROVAL" && p.end > p.start), [model.phases]);
  const waitTotal = waits.reduce((s, w) => s + (w.end - w.start), 0);
  // Compressing only matters when a human wait dominates the run.
  const canCompress = waitTotal > 1000 && waitTotal > 0.25 * model.total;
  const narrow = width > 0 && width < 640;
  const gutter = narrow ? NARROW_GUTTER : WIDE_GUTTER;
  const plotW = Math.max(60, width - gutter - PAD_R);
  const scale = useMemo(
    () => makeScale(model.total, canCompress && compress ? waits : [], plotW),
    [model.total, waits, canCompress, compress, plotW],
  );
  const X = (t: number) => gutter + scale.x(t);

  const roundsH = model.rounds.length ? ROUNDS_H : 0;
  const lanesY = PHASE_H + AXIS_H + 6 + roundsH;
  const lanesBottom = lanesY + model.lanes.length * LANE_H;
  const height = lanesBottom + (model.windows.length ? 34 : 10);
  const laneIndex = new Map(model.lanes.map((l, i) => [l, i]));
  const laneY = (lane: string) => lanesY + (laneIndex.get(lane) ?? 0) * LANE_H;
  const callCount = (lane: string) => model.bars.filter((b) => b.lane === lane).length;
  const requestCount = (lane: string) => model.requests.filter((q) => q.lane === lane).length;

  const show = (x: number, y: number, content: ReactNode) => setTip({ x, y, content });
  const hide = () => setTip(null);

  const laneSub = (lane: string): string => {
    const calls = callCount(lane);
    const asked = requestCount(lane);
    if (lane === "controller") return narrow ? "the only writer" : asked ? `the only writer · ${asked} model req.` : "trusted, the only writer";
    const parts = [`${calls} tool call${calls === 1 ? "" : "s"}`];
    if (asked && !narrow) parts.push(`${asked} model req.`);
    return parts.join(" · ");
  };

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
          <Toggle checked={compress} onChange={setCompress} label="Compress approval wait" hint={`a human took ${fmtMs(waitTotal)} to decide`} />
        ) : null}
        <div className="gantt-legend" aria-label="Legend">
          <span>
            <i className="lg lg-bar" /> tool call
          </span>
          {model.bars.some((b) => b.local) ? (
            <span>
              <i className="lg lg-bar-local" /> runbook search (RECAP's index)
            </span>
          ) : null}
          <span>
            <i className="lg lg-bar-bad" /> failed or denied
          </span>
          <span>
            <i className="lg lg-span" /> agent active
          </span>
          {model.requests.length ? (
            <span>
              <i className={replay ? "lg lg-req replayed" : "lg lg-req"} /> model answer{replay ? " (replayed)" : ""}
            </span>
          ) : null}
          <span>
            <i className="lg lg-dot" /> controller decision
          </span>
          {model.rounds.length ? (
            <span>
              <i className="lg lg-round" /> round
            </span>
          ) : null}
          {model.windows.length ? (
            <span>
              <i className="lg lg-window" /> agents concurrent
            </span>
          ) : null}
        </div>
      </div>

      {view === "table" ? (
        <DataTable
          rows={[...model.bars].sort((a, b) => a.start - b.start)}
          rowKey={(b) => b.id}
          empty="No tool calls recorded for this run."
          columns={[
            { key: "lane", header: "Agent", render: (b) => <Mono>{b.lane}</Mono> },
            {
              key: "tool",
              header: "Tool call",
              render: (b) =>
                b.local ? (
                  <span className="cell-stack">
                    <Mono>{b.tool}</Mono>
                    <span className="muted small">
                      answered by RECAP's runbook index · query (untrusted): <Mono>{str(b.args.query, "—")}</Mono>
                    </span>
                  </span>
                ) : (
                  <Mono>{`${b.tool}(${fmtArgs(b.args)})`}</Mono>
                ),
            },
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
            <svg
              width={width}
              height={height}
              role="group"
              aria-label={`Agent timeline: ${model.bars.length} tool calls and ${model.requests.length} model answers across ${model.lanes.length} lanes over ${fmtMs(model.total)}`}
            >
              {/* lifecycle ribbon */}
              <text x={10} y={PHASE_H / 2 + 4} className="gantt-gutter-label">
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
                    aria-label={`${label}, ${fmtMs(ph.end - ph.start)}`}
                    onPointerEnter={() =>
                      show(
                        x0 + w / 2,
                        PHASE_H,
                        <>
                          <div className="tip-title">{label}</div>
                          <div className="tip-row">
                            <span>from</span>
                            <span>+{fmtMs(ph.start)}</span>
                          </div>
                          <div className="tip-row">
                            <span>for</span>
                            <strong>{fmtMs(ph.end - ph.start)}</strong>
                          </div>
                          {ph.reason ? <div className="tip-note">{ph.reason}</div> : null}
                        </>,
                      )
                    }
                    onFocus={() =>
                      show(
                        x0 + w / 2,
                        PHASE_H,
                        <div className="tip-title">
                          {label} · {fmtMs(ph.end - ph.start)}
                        </div>,
                      )
                    }
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
              <text x={10} y={PHASE_H + 18} className="gantt-gutter-label">
                {narrow ? "time" : replay ? "time in this replay" : "time since alert"}
              </text>
              {scale.ticks.map((t) => (
                <g key={t}>
                  <line x1={X(t)} x2={X(t)} y1={lanesY - 4 - roundsH} y2={lanesBottom} className="gridline" />
                  <text x={X(t)} y={PHASE_H + 18} className="tick-label" textAnchor={t === 0 ? "start" : "middle"}>
                    {tickLabel(t, scale.step)}
                  </text>
                </g>
              ))}

              {/* investigation rounds: a band from each routing decision to the round's last event */}
              {model.rounds.map((r, i) => {
                const x0 = X(r.start);
                const w = Math.max(2, X(r.end) - x0);
                const long = `Round ${r.round} · ${r.mode}`;
                const label = w > long.length * SANS_CHAR + 8 ? long : w > 26 ? `R${r.round}` : "";
                return (
                  <g key={`r${r.round}-${i}`}>
                    <rect x={x0} y={lanesY - roundsH} width={w} height={lanesBottom - lanesY + roundsH} className={i % 2 ? "round-band alt" : "round-band"} />
                    {label ? (
                      <text x={x0 + 4} y={lanesY - 6} className="round-band-label">
                        {label}
                      </text>
                    ) : null}
                  </g>
                );
              })}

              {/* lanes */}
              {model.lanes.map((lane, i) => {
                const y = lanesY + i * LANE_H;
                return (
                  <g key={lane}>
                    <rect x={0} y={y} width={width} height={LANE_H} className={i % 2 ? "lane lane-alt" : "lane"} />
                    <rect x={2} y={y + 9} width={3} height={LANE_H - 18} rx={1.5} className={`lane-key lane-${laneKey(lane === "controller" ? null : lane)}`} />
                    <text x={12} y={y + 21} className="lane-label">
                      {narrow ? laneName(lane === "controller" ? null : lane) : roleLabel(lane)}
                    </text>
                    <text x={12} y={y + 38} className="lane-sub">
                      {laneSub(lane)}
                    </text>
                  </g>
                );
              })}

              {/* concurrency windows: when parallel specialists were active at the same time */}
              {model.windows.map((w, i) => {
                const idx = w.lanes.map((l) => laneIndex.get(l)).filter((v): v is number => v !== undefined);
                const top = idx.length ? lanesY + Math.min(...idx) * LANE_H : lanesY;
                const bottom = idx.length ? lanesY + (Math.max(...idx) + 1) * LANE_H : lanesBottom;
                const x0 = X(w.start);
                const x1 = X(w.end);
                const label = `agents concurrent for ${fmtMs(w.overlapMs ?? w.end - w.start)}`;
                const labelW = label.length * SANS_CHAR;
                const lx = x1 - x0 > labelW ? (x0 + x1) / 2 : Math.max(gutter + labelW / 2, Math.min(x0 + labelW / 2, width - PAD_R - labelW / 2));
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
                const x0 = gutter + b.x0;
                const x1 = gutter + b.x1;
                const mid = (x0 + x1) / 2;
                return (
                  <g key={`b${i}`}>
                    <rect x={x0} y={lanesY - 4} width={Math.max(2, x1 - x0)} height={lanesBottom - lanesY + 4} className="break-band" />
                    <line x1={mid - 6} x2={mid - 1} y1={lanesY + 2} y2={lanesY - 8} className="break-mark" />
                    <line x1={mid + 1} x2={mid + 6} y1={lanesY + 2} y2={lanesY - 8} className="break-mark" />
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
                const content = <BarTip bar={bar} author={author} />;
                return (
                  <g
                    key={bar.id}
                    className="gantt-hit"
                    tabIndex={0}
                    aria-label={`${bar.lane}: ${bar.tool}${bar.local ? " (answered by RECAP's runbook index)" : ""}, ${fmtMs(bar.durationMs)}`}
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
                      className={denied ? "bar bar-bad" : bar.status === "running" ? "bar bar-running" : bar.local ? "bar bar-local" : "bar"}
                    />
                    {fits ? (
                      <text x={x0 + 5} y={y + BAR_H / 2 + 4} className="bar-label">
                        {bar.tool}
                      </text>
                    ) : null}
                  </g>
                );
              })}

              {/* model answers: a diamond where the trace recorded each one */}
              {model.requests.map((q) => {
                const cx = X(q.t);
                const cy = laneY(q.lane) + 12;
                const content = <RequestTip mark={q} />;
                return (
                  <g
                    key={q.id}
                    className="gantt-hit"
                    tabIndex={0}
                    aria-label={`Model answer for ${q.lane}${q.step !== null ? `, step ${q.step}` : ""}: ${fmtMs(q.durationMs)}${q.replayed ? " (original run)" : ""}`}
                    onPointerEnter={() => show(cx, cy - 6, content)}
                    onFocus={() => show(cx, cy - 6, content)}
                    onBlur={hide}
                    onPointerLeave={hide}
                  >
                    <circle cx={cx} cy={cy} r={10} className="hit" />
                    <path
                      d={`M ${cx} ${cy - 5} L ${cx + 5} ${cy} L ${cx} ${cy + 5} L ${cx - 5} ${cy} Z`}
                      className={["req-mark", q.replayed ? "replayed" : "", q.ok ? "" : "failed"].filter(Boolean).join(" ")}
                    />
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
                    <div className="tip-row">
                      <span>at</span>
                      <span>+{fmtMs(m.t)}</span>
                    </div>
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
            <div className={`gantt-tip${tip.x > width - 170 ? " right" : tip.x < 170 ? " left" : ""}`} style={{ left: tip.x, top: tip.y }} role="tooltip">
              {tip.content}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

// ======================================================================================
// One round: each agent's turn on the round's own time axis
// ======================================================================================

/**
 * The agents' turns in one round as bars on the round's time axis, with the window where they
 * ran at the same time shaded. Bars come from the lanes' recorded start and finish; the overlap
 * figure is the orchestrator's measurement (`overlapMs`), shown next to it.
 */
/** A position on an axis: "0 ms" at the origin (fmtMs reads "<1 ms" for a duration that small). */
function offsetText(ms: number): string {
  return ms < 0.5 ? "0 ms" : fmtMs(ms);
}

export function RoundLanes({
  geometry,
  overlapMs,
  round,
  replay = false,
}: {
  geometry: RoundGeometry;
  overlapMs: number | null;
  round: number;
  replay?: boolean;
}) {
  const span = geometry.span > 0 ? geometry.span : 1;
  const pct = (ms: number) => `${Math.max(0, Math.min(100, (ms / span) * 100))}%`;
  const overlap = geometry.overlap;
  return (
    <div className="tl-lanes" role="group" aria-label={`Agent turns in round ${round}`}>
      {geometry.bars.map((bar) => {
        const end = bar.end ?? geometry.span;
        const width = Math.max(0, end - bar.start);
        return (
          <div className="tl-lanes-row" key={bar.agent}>
            <span className="tl-lanes-name">
              <span className={`tl-lane-key lane-${laneKey(bar.agent)}`} aria-hidden="true" />
              {laneName(bar.agent)}
            </span>
            <span className="tl-lanes-track" aria-hidden="true">
              {overlap ? <span className="tl-lanes-overlap" style={{ left: pct(overlap.start), width: pct(overlap.end - overlap.start) }} /> : null}
              <span
                className={`tl-lanes-bar lane-${laneKey(bar.agent)}${bar.end === null ? " open" : ""}`}
                style={{ left: pct(bar.start), width: `max(3px, ${pct(width)})` }}
              />
            </span>
            <span className="tl-lanes-time">
              {bar.end === null ? (
                <span className="not-recorded">not finished</span>
              ) : (
                <>
                  {fmtMs(bar.lane.duration_ms ?? width)}
                  <span className="sr-only">
                    {" "}
                    from +{offsetText(bar.start)} to +{offsetText(bar.end)}
                  </span>
                </>
              )}
            </span>
          </div>
        );
      })}
      <div className="tl-lanes-foot">
        <span>
          {geometry.bars.length < 2
            ? "One agent: nothing to overlap."
            : overlapMs !== null && overlapMs > 0
              ? `Turns overlapped for ${fmtMs(overlapMs)} (measured by the orchestrator).`
              : overlapMs !== null
                ? "Parallel round, but the turns did not overlap (measured)."
                : overlap
                  ? `Turns overlapped for about ${fmtMs(overlap.end - overlap.start)} by their recorded start and finish; the orchestrator's measurement is not recorded.`
                  : "The turns did not overlap."}
        </span>
        <span className="muted">
          axis 0 – {fmtMs(geometry.span)}
          {replay ? " · this replay's own time" : ""}
        </span>
      </div>
    </div>
  );
}

// ======================================================================================
// One model request: where its time went
// ======================================================================================

const PART_LABELS: Record<string, string> = {
  compute: "model compute",
  wait: "waiting inside the server",
  server: "server time",
  outside: "outside the server",
  total: "whole request (not broken down)",
};

/** A stacked bar of a model request's recorded time: compute, waiting inside the server, outside it. */
export function RequestBar({ split }: { split: RequestSplit }) {
  return (
    <span className="tl-reqbar" aria-hidden="true">
      {split.parts.map((part) =>
        part.share > 0 ? (
          <span key={part.key} className={`tl-reqbar-part part-${part.key}`} style={{ width: `${part.share * 100}%` }} title={`${PART_LABELS[part.key]}: ${fmtMs(part.ms)}`} />
        ) : null,
      )}
    </span>
  );
}
