/**
 * View helpers for the Multi-Agent Trace Explorer (#/timeline), built on GET /runs/{id}/timeline.
 *
 * The server sends a run's trace in pages (cursor paging: `after_seq` -> `next_after_seq`,
 * `has_more`), each page with the run's agent lanes, rounds and counts computed over the whole
 * trace. These helpers:
 *
 *   - merge pages: the round views load the whole trace once (in pages of up to 2,000 events),
 *     the event log pages through it 100 events at a time with the server's filters;
 *   - cut the trace into the run's story: what happened before the first round, each round, and
 *     what followed it (the fix, its checks, the approval, the write and the oracle);
 *   - group one agent's turn into its steps (context pack -> model request -> decision -> tool
 *     calls, each paired with the gateway's verdict and its result);
 *   - split a model request's time into the server's compute, the wait inside the server and the
 *     time outside it, exactly as recorded (a part the server did not report stays null);
 *   - place each agent's turn on its round's time axis, with the window where turns overlapped.
 *
 * Nothing is inferred that the server did not record: a missing value is null ("Not recorded"),
 * never a default dressed as data. Text a model, a tool or an alert wrote stays a plain string:
 * the page shows it as untrusted, quoted data.
 *
 * Pure functions with type-only imports, so the Node tests in ../../tests load this file directly.
 */

import type { AgentLane, ModelRequest, RoundInfo, TimelineEntry, TimelineKind, TimelineView } from "../dto";
import type { TraceEvent } from "../types";

// --- defensive readers (local copies: this module imports no runtime values) ------------------

function list<T = unknown>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function text(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function fields(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/** A Python ISO timestamp in ms (microseconds trimmed to milliseconds, like format.ts tsMs). */
export function timeOf(iso: string | null | undefined): number {
  if (!iso) return Number.NaN;
  return Date.parse(iso.replace(/(\.\d{3})\d+/, "$1"));
}

// --- event kinds -------------------------------------------------------------------------------

/** What each TimelineKind (apps/api/views.py `_KIND`) is called on screen. */
export const KIND_LABELS: Readonly<Record<TimelineKind, string>> = {
  lifecycle: "Lifecycle",
  tool_discovery: "MCP tool discovery",
  retrieval: "Runbook retrieval",
  memory: "Experience memory",
  routing: "Routing decision",
  round_start: "Round start",
  agent_start: "Agent start",
  context: "Context pack",
  model_request: "Model request",
  agent_decision: "Agent decision",
  policy: "Policy verdict",
  tool_call: "Tool call",
  tool_result: "Tool result",
  injection_flag: "Injection flag",
  agent_finish: "Agent finish",
  round_summary: "Round summary",
  verification: "Verification",
  proposal: "Fix proposal",
  approval_requested: "Approval requested",
  approval_decided: "Approval decided",
  write_started: "Write started",
  write_executed: "Write executed",
  oracle: "Recovery oracle",
  budget: "Budget stop",
  error: "Error",
  summary: "Run summary",
  other: "Other",
};

export const TIMELINE_KINDS: readonly TimelineKind[] = Object.keys(KIND_LABELS) as TimelineKind[];

export function isTimelineKind(value: unknown): value is TimelineKind {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(KIND_LABELS, value);
}

export function kindLabel(kind: string | null | undefined): string {
  return isTimelineKind(kind) ? KIND_LABELS[kind] : kind ? kind.replace(/_/g, " ") : "Other";
}

// --- lanes -------------------------------------------------------------------------------------

/** The lanes in reading order: the specialists, the single investigator, then the controller. */
export const LANE_ORDER: readonly string[] = [
  "telemetry_investigator",
  "infrastructure_investigator",
  "investigator",
  "controller",
];

/** "telemetry_investigator" -> "Telemetry"; no agent (RECAP's own events) -> "Controller". */
export function laneName(agent: string | null | undefined): string {
  if (!agent || agent === "controller") return "Controller";
  const suffix = "_investigator";
  const base = agent.endsWith(suffix) ? agent.slice(0, -suffix.length) : agent;
  const words = base.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A CSS-safe lane key for colouring ("telemetry", "infrastructure", "investigator", "controller", "other"). */
export function laneKey(agent: string | null | undefined): string {
  if (!agent || agent === "controller") return "controller";
  if (agent === "telemetry_investigator") return "telemetry";
  if (agent === "infrastructure_investigator") return "infrastructure";
  if (agent === "investigator") return "investigator";
  return "other";
}

export function laneRank(agent: string | null | undefined): number {
  const at = LANE_ORDER.indexOf(agent || "controller");
  return at === -1 ? LANE_ORDER.length : at;
}

// --- the shape the older helpers read ------------------------------------------------------------

/** A timeline entry as the TraceEvent that describeEvent, buildRounds and buildTimeline read. */
export function toTraceEvent(entry: TimelineEntry, runId: string): TraceEvent {
  return {
    event_id: `${runId}#${entry.seq}`,
    run_id: runId,
    seq: entry.seq,
    stage: entry.stage,
    ts: entry.ts,
    agent: entry.agent,
    category: entry.category,
    payload: entry.payload ?? {},
    duration_ms: entry.duration_ms,
    usage: null,
    schema_version: "",
  };
}

/** Pages merged by `seq`: no duplicates, in order. The same array comes back when nothing is new. */
export function mergeEntries(current: readonly TimelineEntry[], incoming: readonly TimelineEntry[]): readonly TimelineEntry[] {
  if (!incoming.length) return current;
  const seen = new Set(current.map((e) => e.seq));
  const added: TimelineEntry[] = [];
  for (const entry of incoming) {
    if (seen.has(entry.seq)) continue;
    seen.add(entry.seq);
    added.push(entry);
  }
  if (!added.length) return current;
  const merged = [...current, ...added];
  for (let i = 1; i < merged.length; i += 1) {
    if (merged[i].seq < merged[i - 1].seq) {
      merged.sort((a, b) => a.seq - b.seq);
      break;
    }
  }
  return merged;
}

// --- loading the whole trace (for the round views) ---------------------------------------------------

/** The endpoint's largest page (GET /runs/{id}/timeline `limit` <= 2000). */
export const TRACE_PAGE_SIZE = 2000;
/** The round views stop loading here; the event log still pages through every event. */
export const ANALYSIS_LIMIT = 10_000;

export interface TraceState {
  /** The latest page: lanes, rounds, counts and notes are the server's, over the whole trace. */
  view: TimelineView | null;
  entries: readonly TimelineEntry[];
  /** after_seq of the next request. */
  cursor: number;
  /** Every stored event is loaded (a live run may add more). */
  complete: boolean;
  /** Loading stopped at ANALYSIS_LIMIT events: the round views use the first ones only. */
  truncated: boolean;
}

export const EMPTY_TRACE: TraceState = { view: null, entries: [], cursor: 0, complete: false, truncated: false };

export function addTracePage(state: TraceState, page: TimelineView, limit = ANALYSIS_LIMIT): TraceState {
  const entries = mergeEntries(state.entries, page.entries ?? []);
  const cursor = Math.max(state.cursor, num(page.next_after_seq) ?? state.cursor);
  const truncated = !!page.has_more && entries.length >= limit;
  return { view: page, entries, cursor, complete: !page.has_more, truncated };
}

/** What the loader does next: fetch the next page now, poll a live run in a while, or stop. */
export function traceNext(state: TraceState): "page" | "poll" | "stop" {
  if (!state.view) return "page";
  if (state.truncated) return "stop";
  if (!state.complete) return "page";
  return state.view.live ? "poll" : "stop";
}

// --- the event log (server filters, cursor paging) ----------------------------------------------------

export const LOG_PAGE_SIZE = 100;

export interface LogFilters {
  agent: string | null;
  round: number | null;
  kind: TimelineKind | null;
  tool: string | null;
}

export const NO_FILTERS: LogFilters = { agent: null, round: null, kind: null, tool: null };

/** The event log's filters from the route (`?agent=&round=&kind=&tool=`): a value the API would refuse is dropped. */
export function readLogFilters(get: (key: string) => string | null | undefined): LogFilters {
  const agent = (get("agent") ?? "").trim();
  const tool = (get("tool") ?? "").trim();
  const roundText = (get("round") ?? "").trim();
  const round = /^\d+$/.test(roundText) ? Number(roundText) : null;
  const kind = get("kind");
  return {
    agent: agent || null,
    round: round !== null && round >= 1 ? round : null,
    kind: isTimelineKind(kind) ? kind : null,
    tool: tool || null,
  };
}

export function activeLogFilters(filters: LogFilters): number {
  return [filters.agent, filters.round, filters.kind, filters.tool].filter((v) => v !== null).length;
}

/** The query of one event-log page (GET /runs/{id}/timeline). */
export function logQuery(filters: LogFilters, afterSeq: number, limit = LOG_PAGE_SIZE) {
  return {
    after_seq: afterSeq,
    limit,
    agent: filters.agent ?? undefined,
    round: filters.round ?? undefined,
    kind: filters.kind ?? undefined,
    tool: filters.tool ?? undefined,
  };
}

export interface LogState {
  entries: readonly TimelineEntry[];
  cursor: number;
  hasMore: boolean;
  /** Events that pass the filters, over the whole trace (the server's count). */
  matching: number | null;
  /** Every stored event of the run. */
  total: number | null;
  live: boolean;
  loaded: boolean;
}

export const EMPTY_LOG: LogState = { entries: [], cursor: 0, hasMore: false, matching: null, total: null, live: false, loaded: false };

export function addLogPage(state: LogState, page: TimelineView): LogState {
  return {
    entries: mergeEntries(state.entries, page.entries ?? []),
    cursor: Math.max(state.cursor, num(page.next_after_seq) ?? state.cursor),
    hasMore: !!page.has_more,
    matching: num(page.matching_events),
    total: num(page.total_events),
    live: !!page.live,
    loaded: true,
  };
}

// --- the run's story: before the first round, each round, after it ---------------------------------

export interface Segment {
  key: string;
  /** setup: before the first round · round: one investigation round · after: what followed a round. */
  kind: "setup" | "round" | "after";
  /** kind "round": its number. */
  round: number | null;
  /** kind "after": the round it followed. */
  after: number | null;
  entries: TimelineEntry[];
}

/**
 * The trace cut into its story, in order. A round's events carry its number (the server's
 * `round`); an event without one that sits between two events of the same round stays in it.
 * Rounds continue across a failed fix (round 3 follows the fix after round 2).
 */
export function segmentsOf(entries: readonly TimelineEntry[]): Segment[] {
  const sorted = [...entries].sort((a, b) => a.seq - b.seq);
  // The round of the next event that names one, for each position.
  const next: (number | null)[] = new Array(sorted.length).fill(null);
  let upcoming: number | null = null;
  for (let i = sorted.length - 1; i >= 0; i -= 1) {
    next[i] = upcoming;
    if (sorted[i].round !== null && sorted[i].round !== undefined) upcoming = sorted[i].round;
  }
  const out: Segment[] = [];
  let current: Segment | null = null;
  let lastRound: number | null = null;
  for (let i = 0; i < sorted.length; i += 1) {
    const entry = sorted[i];
    let round: number | null = entry.round ?? null;
    if (round === null && current !== null && current.kind === "round" && next[i] === current.round) round = current.round;
    if (round !== null) {
      if (current === null || current.kind !== "round" || current.round !== round) {
        current = { key: `round-${round}-${entry.seq}`, kind: "round", round, after: null, entries: [] };
        out.push(current);
      }
      lastRound = round;
    } else if (current === null || current.kind === "round") {
      current =
        lastRound === null
          ? { key: `setup-${entry.seq}`, kind: "setup", round: null, after: null, entries: [] }
          : { key: `after-${entry.seq}`, kind: "after", round: null, after: lastRound, entries: [] };
      out.push(current);
    }
    current.entries.push(entry);
  }
  return out;
}

/** Why a round was escalated, read from what the trace recorded before it. */
export type Escalation =
  | { kind: "none" }
  | { kind: "not_recorded" }
  /** The round before it ended without a supported root cause (the verifier's status). */
  | { kind: "inconclusive"; round: number; status: string | null }
  /** A fix before it did not recover the system (the oracle said so). */
  | { kind: "failed_fix" }
  /** Escalated, but the trace before it does not show why (e.g. a trace cut short). */
  | { kind: "recorded" };

export function escalationOf(
  info: Pick<RoundInfo, "escalation"> | null | undefined,
  segments: readonly Segment[],
  index: number,
  rounds: readonly Pick<RoundInfo, "round" | "verification_status">[],
): Escalation {
  const flag = info?.escalation;
  if (flag === false) return { kind: "none" };
  if (flag === null || flag === undefined) return { kind: "not_recorded" };
  const before = segments[index - 1];
  if (before?.kind === "after" && before.entries.some((e) => e.kind === "oracle" && e.ok === false)) return { kind: "failed_fix" };
  if (before?.kind === "round" && before.round !== null) {
    const status = rounds.find((r) => r.round === before.round)?.verification_status ?? null;
    return { kind: "inconclusive", round: before.round, status };
  }
  return { kind: "recorded" };
}

// --- one agent's turn, step by step ------------------------------------------------------------------

export interface ToolStep {
  key: string;
  tool: string;
  invocationId: string | null;
  /** The gateway's verdict on the call (policy.decision); null when none was recorded. */
  policy: TimelineEntry | null;
  call: TimelineEntry | null;
  result: TimelineEntry | null;
}

export interface AgentStep {
  key: string;
  /** The harness's step number; null when the trace did not number it. */
  step: number | null;
  context: TimelineEntry | null;
  /** Usually one; a corrective retry adds another (attempt 2). */
  requests: TimelineEntry[];
  decision: TimelineEntry | null;
  tools: ToolStep[];
  /** The agent's other events in this step: its runbook searches' results, injection flags, ... */
  other: TimelineEntry[];
}

export interface AgentTurn {
  agent: string;
  start: TimelineEntry | null;
  finish: TimelineEntry | null;
  steps: AgentStep[];
}

const STEP_KINDS: ReadonlySet<string> = new Set(["context", "model_request", "agent_decision"]);

/**
 * Each agent's turn in one round (pass the round's entries), with its steps in order. A step
 * starts at a context pack, or at a model request or decision numbered differently from the
 * step before. The gateway's verdict is emitted just before its call: verdicts pair with the
 * agent's next call of the same tool, in order; a verdict that is not ALLOW (no call follows)
 * stands alone. A result pairs with its call by invocation id.
 */
export function agentTurns(entries: readonly TimelineEntry[]): Map<string, AgentTurn> {
  const turns = new Map<string, AgentTurn>();
  const pending = new Map<string, TimelineEntry[]>(); // agent|tool -> ALLOW verdicts waiting for their call
  const turnOf = (agent: string): AgentTurn => {
    let turn = turns.get(agent);
    if (!turn) {
      turn = { agent, start: null, finish: null, steps: [] };
      turns.set(agent, turn);
    }
    return turn;
  };
  const newStep = (turn: AgentTurn, step: number | null, entry: TimelineEntry): AgentStep => {
    const made: AgentStep = { key: `${turn.agent}-${entry.seq}`, step, context: null, requests: [], decision: null, tools: [], other: [] };
    turn.steps.push(made);
    return made;
  };
  const currentStep = (turn: AgentTurn, entry: TimelineEntry): AgentStep =>
    turn.steps[turn.steps.length - 1] ?? newStep(turn, null, entry);

  for (const entry of [...entries].sort((a, b) => a.seq - b.seq)) {
    const agent = entry.agent;
    if (!agent || agent === "controller") continue;
    const turn = turnOf(agent);
    if (entry.kind === "agent_start") {
      turn.start ??= entry;
      continue;
    }
    if (entry.kind === "agent_finish") {
      turn.finish = entry;
      continue;
    }
    if (STEP_KINDS.has(entry.kind)) {
      const last = turn.steps[turn.steps.length - 1];
      const number = entry.step ?? null;
      const fresh =
        !last ||
        entry.kind === "context" ||
        (number !== null && last.step !== null && number !== last.step) ||
        (entry.kind === "agent_decision" && last.decision !== null) ||
        (entry.kind === "model_request" && last.decision !== null);
      const step = fresh ? newStep(turn, number, entry) : last;
      if (step.step === null && number !== null) step.step = number;
      if (entry.kind === "context") step.context = entry;
      else if (entry.kind === "model_request") step.requests.push(entry);
      else step.decision = entry;
      continue;
    }
    if (entry.kind === "policy") {
      const tool = entry.tool ?? "?";
      if (entry.verdict === "ALLOW") {
        const key = `${agent}|${tool}`;
        pending.set(key, [...(pending.get(key) ?? []), entry]);
      } else {
        currentStep(turn, entry).tools.push({ key: `p${entry.seq}`, tool, invocationId: null, policy: entry, call: null, result: null });
      }
      continue;
    }
    if (entry.kind === "tool_call") {
      const tool = entry.tool ?? "?";
      const key = `${agent}|${tool}`;
      const waiting = pending.get(key) ?? [];
      const policy = waiting.shift() ?? null;
      pending.set(key, waiting);
      currentStep(turn, entry).tools.push({ key: `c${entry.seq}`, tool, invocationId: entry.invocation_id, policy, call: entry, result: null });
      continue;
    }
    if (entry.kind === "tool_result") {
      const found = findCall(turn, entry);
      if (found) found.result = entry;
      else currentStep(turn, entry).tools.push({ key: `r${entry.seq}`, tool: entry.tool ?? "?", invocationId: entry.invocation_id, policy: null, call: null, result: entry });
      continue;
    }
    currentStep(turn, entry).other.push(entry);
  }
  // An ALLOW with no call after it (a trace cut short): shown on its own, never dropped.
  for (const [key, waiting] of pending) {
    const agent = key.slice(0, key.indexOf("|"));
    const turn = turns.get(agent);
    if (!turn) continue;
    for (const policy of waiting) {
      currentStep(turn, policy).tools.push({ key: `p${policy.seq}`, tool: policy.tool ?? "?", invocationId: null, policy, call: null, result: null });
    }
  }
  return turns;
}

function findCall(turn: AgentTurn, result: TimelineEntry): ToolStep | null {
  const id = result.invocation_id;
  for (let s = turn.steps.length - 1; s >= 0; s -= 1) {
    const tools = turn.steps[s].tools;
    for (let t = tools.length - 1; t >= 0; t -= 1) {
      const tool = tools[t];
      if (tool.result || !tool.call) continue;
      if (id ? tool.invocationId === id : tool.tool === result.tool) return tool;
    }
  }
  return null;
}

/** Tool calls in a turn (every call event, local runbook searches included). */
export function turnToolCalls(turn: AgentTurn): number {
  return turn.steps.reduce((sum, step) => sum + step.tools.filter((t) => t.call).length, 0);
}

// --- model requests ---------------------------------------------------------------------------------

export type RequestPartKey = "compute" | "wait" | "server" | "outside" | "total";

export interface RequestPart {
  key: RequestPartKey;
  ms: number;
  /** Share of the request's whole time (0..1). */
  share: number;
}

export interface RequestSplit {
  /** duration_ms: from sending the request to its answer, as RECAP measured it. */
  totalMs: number;
  /** The server's own time (null: the server did not report one). */
  serverMs: number | null;
  /** Load + prompt evaluation + generation, as the server reported it. */
  computeMs: number | null;
  /** server_ms minus its compute: waiting inside the server (e.g. queued behind another agent). */
  waitMs: number | null;
  /** duration_ms minus server_ms: connection and transfer. Not a queue. */
  outsideMs: number | null;
  /** The recorded parts, in order, for a stacked bar. */
  parts: RequestPart[];
}

type RequestTimes = Pick<ModelRequest, "duration_ms" | "server_ms" | "server_compute_ms" | "server_wait_ms" | "outside_server_ms">;

export function requestSplit(mr: RequestTimes): RequestSplit {
  const total = Math.max(0, num(mr.duration_ms) ?? 0);
  const server = num(mr.server_ms);
  const compute = num(mr.server_compute_ms ?? null);
  const wait = num(mr.server_wait_ms ?? null);
  const outside = num(mr.outside_server_ms);
  const share = (ms: number) => (total > 0 ? Math.max(0, Math.min(1, ms / total)) : 0);
  const parts: RequestPart[] = [];
  if (server !== null && compute !== null && wait !== null) {
    parts.push({ key: "compute", ms: compute, share: share(compute) }, { key: "wait", ms: wait, share: share(wait) });
  } else if (server !== null) {
    parts.push({ key: "server", ms: server, share: share(server) });
  } else {
    parts.push({ key: "total", ms: total, share: total > 0 ? 1 : 0 });
  }
  if (server !== null && outside !== null && outside > 0) parts.push({ key: "outside", ms: outside, share: share(outside) });
  return { totalMs: total, serverMs: server, computeMs: compute, waitMs: wait, outsideMs: outside, parts };
}

export interface RequestTotals {
  count: number;
  /** Corrective retries (attempt > 1). */
  retries: number;
  /** Requests that gave no usable answer. */
  failed: number;
  /** Answered from a recording: their times and counts are the original run's. */
  replayed: number;
  /** Sums over the requests whose server reported a count; null when none did. */
  inputTokens: number | null;
  outputTokens: number | null;
  /** Requests whose server reported no token counts. */
  unreported: number;
  durationMs: number;
  /** Sum over the requests that report a server time; null when none does. */
  serverMs: number | null;
  /** Sum of the recorded waits inside the server; null when none is recorded. */
  waitMs: number | null;
  /** Requests whose wait inside the server is recorded. */
  withWait: number;
}

export function requestTotals(entries: readonly TimelineEntry[]): RequestTotals {
  const totals: RequestTotals = {
    count: 0,
    retries: 0,
    failed: 0,
    replayed: 0,
    inputTokens: null,
    outputTokens: null,
    unreported: 0,
    durationMs: 0,
    serverMs: null,
    waitMs: null,
    withWait: 0,
  };
  for (const entry of entries) {
    const mr = entry.model_request;
    if (!mr) continue;
    totals.count += 1;
    if (mr.retry || mr.attempt > 1) totals.retries += 1;
    if (!mr.ok) totals.failed += 1;
    if (mr.replayed) totals.replayed += 1;
    const input = num(mr.input_tokens);
    const output = num(mr.output_tokens);
    if (input === null && output === null) totals.unreported += 1;
    if (input !== null) totals.inputTokens = (totals.inputTokens ?? 0) + input;
    if (output !== null) totals.outputTokens = (totals.outputTokens ?? 0) + output;
    totals.durationMs += num(mr.duration_ms) ?? 0;
    const server = num(mr.server_ms);
    if (server !== null) totals.serverMs = (totals.serverMs ?? 0) + server;
    const wait = num(mr.server_wait_ms ?? null);
    if (wait !== null) {
      totals.waitMs = (totals.waitMs ?? 0) + wait;
      totals.withWait += 1;
    }
  }
  return totals;
}

// --- each agent's turn on its round's time axis ---------------------------------------------------

export interface LaneBar {
  agent: string;
  lane: AgentLane;
  /** ms from the round's first start. */
  start: number;
  /** null: the turn has not finished (or never did). */
  end: number | null;
}

export interface RoundGeometry {
  /** ms from the first start to the last finish (or the latest start when none finished). */
  span: number;
  bars: LaneBar[];
  /** Where all the turns ran at the same time, from their recorded start and finish; null when they did not. */
  overlap: { start: number; end: number } | null;
}

/** `order`: the router's order of the agents (the lanes are drawn in it). */
export function roundGeometry(lanes: readonly AgentLane[], order: readonly string[] = []): RoundGeometry | null {
  const timed = lanes.filter((lane) => Number.isFinite(timeOf(lane.started_at)));
  if (!timed.length) return null;
  const t0 = Math.min(...timed.map((lane) => timeOf(lane.started_at)));
  const rank = (agent: string) => {
    const at = order.indexOf(agent);
    return at === -1 ? order.length + laneRank(agent) : at;
  };
  const bars: LaneBar[] = timed
    .map((lane) => {
      const start = timeOf(lane.started_at) - t0;
      const finished = timeOf(lane.finished_at);
      return { agent: lane.agent, lane, start, end: Number.isFinite(finished) ? Math.max(start, finished - t0) : null };
    })
    .sort((a, b) => rank(a.agent) - rank(b.agent) || a.agent.localeCompare(b.agent));
  const span = Math.max(0, ...bars.map((b) => b.end ?? b.start));
  let overlap: RoundGeometry["overlap"] = null;
  if (bars.length >= 2 && bars.every((b) => b.end !== null)) {
    const start = Math.max(...bars.map((b) => b.start));
    const end = Math.min(...bars.map((b) => b.end as number));
    if (end > start) overlap = { start, end };
  }
  return { span, bars, overlap };
}

// --- context packs -----------------------------------------------------------------------------------

export interface ContextPack {
  step: number | null;
  /** "proposal" for the pack the fix was drafted from; null for an agent's step. */
  stage: string | null;
  tokensBefore: number | null;
  tokensAfter: number | null;
  budget: number | null;
  items: string[];
  dropped: string[];
  /** Token counts are RECAP's estimate (never a model's count). */
  estimate: boolean;
}

export function contextPack(entry: Pick<TimelineEntry, "payload" | "step">): ContextPack {
  const p = fields(entry.payload);
  return {
    step: entry.step ?? num(p.step),
    stage: text(p.stage) || null,
    tokensBefore: num(p.tokens_before),
    tokensAfter: num(p.tokens_after),
    budget: num(p.budget_tokens),
    items: list(p.items).map(text).filter(Boolean),
    dropped: list(p.dropped).map(text).filter(Boolean),
    estimate: p.estimate !== false,
  };
}

/** An item in a context pack: an evidence ID (links to Evidence & Decisions), a runbook chunk, or other. */
export function itemKind(id: string): "evidence" | "runbook" | "other" {
  if (/^ev-/.test(id)) return "evidence";
  if (id.includes("#")) return "runbook";
  return "other";
}

// --- the run's own facts -------------------------------------------------------------------------------

/** The lifecycle reason that names the diagnosis repeats the claim an agent wrote: split it off. */
export function diagnosisReason(reason: string): { lead: string; claim: string } | null {
  const marker = "diagnosis: ";
  return reason.startsWith(marker) ? { lead: "diagnosis", claim: reason.slice(marker.length) } : null;
}

/** The first event's time (ms): every offset on the page counts from it. */
export function traceStart(entries: readonly Pick<TimelineEntry, "ts">[]): number | null {
  for (const entry of entries) {
    const t = timeOf(entry.ts);
    if (Number.isFinite(t)) return t;
  }
  return null;
}

/** Offsets of the events, from the first event of the trace. */
export function offsetOf(entry: Pick<TimelineEntry, "ts">, start: number | null): number | null {
  if (start === null) return null;
  const t = timeOf(entry.ts);
  return Number.isFinite(t) ? Math.max(0, t - start) : null;
}

/** Options for the event log's filters, with counts, from the loaded trace (the server filters). */
export interface FilterCounts {
  agents: Map<string, number>;
  rounds: Map<number, number>;
  tools: Map<string, number>;
}

export function filterCounts(entries: readonly TimelineEntry[]): FilterCounts {
  const counts: FilterCounts = { agents: new Map(), rounds: new Map(), tools: new Map() };
  for (const entry of entries) {
    if (entry.agent) counts.agents.set(entry.agent, (counts.agents.get(entry.agent) ?? 0) + 1);
    if (entry.round !== null && entry.round !== undefined) counts.rounds.set(entry.round, (counts.rounds.get(entry.round) ?? 0) + 1);
    if (entry.tool) counts.tools.set(entry.tool, (counts.tools.get(entry.tool) ?? 0) + 1);
  }
  return counts;
}

/** The agent lanes of one round, in the router's order. */
export function lanesOf(lanes: readonly AgentLane[], round: number): AgentLane[] {
  return lanes.filter((lane) => lane.round === round);
}

/** The largest measured overlap of agent turns in any round (null when no round recorded one). */
export function bestOverlap(rounds: readonly Pick<RoundInfo, "overlap_ms">[]): number | null {
  const measured = rounds.map((r) => num(r.overlap_ms)).filter((v): v is number => v !== null);
  return measured.length ? Math.max(...measured) : null;
}

/** The model requests in a set of entries were all answered from a recording. */
export function allReplayed(entries: readonly TimelineEntry[]): boolean {
  const requests = entries.filter((e) => e.model_request);
  return requests.length > 0 && requests.every((e) => e.model_request?.replayed === true);
}
