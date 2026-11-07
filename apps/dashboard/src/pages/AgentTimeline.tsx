import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, toApiError } from "../api";
import type { ApiError } from "../api";
import { Chip, FlagChip, HypothesisChip, Mono, OriginChip } from "../components/Badge";
import { Card, KeyValues, Meter, Notice, PageHeader, StatTile } from "../components/Card";
import { DataSourceBadge, summarySource } from "../components/DataSourceBadge";
import { FilterBar } from "../components/FilterBar";
import type { FilterOption, FilterSpec } from "../components/FilterBar";
import { Icon } from "../components/Icon";
import type { IconName } from "../components/Icon";
import { JsonView } from "../components/JsonView";
import { RunScope } from "../components/RunPicker";
import { EmptyView, ErrorView, LoadingView } from "../components/StateViews";
import { StateToken, StatusToken } from "../components/StatusToken";
import { AgentGantt, RequestBar, RoundLanes, buildTimeline } from "../components/Timeline";
import { UntrustedText } from "../components/Untrusted";
import type * as dto from "../dto";
import {
  arr,
  budgetUses,
  describeEvent,
  fmtArgs,
  fmtDateTime,
  fmtInt,
  fmtMs,
  fmtScore,
  humanize,
  hypothesisTone,
  isTerminal,
  modelNotesByAgent,
  num,
  obj,
  riskTone,
  roleLabel,
  shortDigest,
  splitSummary,
  stateLabel,
  stateTone,
  str,
  verdictLabel,
  verdictTone,
  yesNo,
} from "../format";
import type { EventLine, FeedContext, Tone } from "../format";
import { hrefFor, navigate, paramsOf, usePolling, useRunSelection } from "../hooks";
import type { PageProps, Polled, Route } from "../hooks";
import type {
  ApprovalDecidedPayload,
  ApprovalRequestedPayload,
  BudgetPayload,
  DocRef,
  EvidenceCheckPayload,
  InjectionPayload,
  MemorySearchPayload,
  MemoryWritePayload,
  MitigationExecutedPayload,
  MitigationProposal,
  OrchestrationAgent,
  OrchestrationPayload,
  PolicyPayload,
  ProposalPayload,
  ProviderInfo,
  RecoveryPayload,
  RetrievalPayload,
  RoutingPayload,
  RunRecord,
  RunSummaryPayload,
  StatePayload,
  TraceEvent,
} from "../types";
import { buildRounds, droppedLine, firstIdSuffix, isRunbookSearch, shortRole, textAuthor } from "../view/findings";
import type {
  AgentView,
  CheckView,
  ClaimView,
  EvidenceRef,
  FinalVerdict,
  JudgedBy,
  RoundView,
  RunbookSearch,
  TextAuthor,
  VerifierStep,
} from "../view/findings";
import { liveTargetWords, settleWords, summaryTarget, targetFacts, targetPlace } from "../view/lab";
import type { TargetFacts, TargetFields } from "../view/lab";
import { isOverlayPath, knowledgeOverlays, overlayChunkIds, searchesOf } from "../view/retrieval";
import {
  EMPTY_LOG,
  EMPTY_TRACE,
  LOG_PAGE_SIZE,
  TRACE_PAGE_SIZE,
  activeLogFilters,
  addLogPage,
  addTracePage,
  agentTurns,
  allReplayed,
  bestOverlap,
  contextPack,
  diagnosisReason,
  escalationOf,
  filterCounts,
  itemKind,
  kindLabel,
  laneKey,
  laneName,
  laneRank,
  lanesOf,
  logQuery,
  offsetOf,
  readLogFilters,
  requestSplit,
  requestTotals,
  roundGeometry,
  segmentsOf,
  toTraceEvent,
  traceNext,
  traceStart,
  turnToolCalls,
} from "../view/timeline";
import type { AgentStep, AgentTurn, Escalation, LogFilters, LogState, RequestTotals, Segment, ToolStep, TraceState } from "../view/timeline";
import { detailLabel, isCheckedDetail, ruleNote } from "../view/verification";
import "../styles/timeline-m3.css";

/** A live run's trace is polled this often (only the events after the last one loaded). */
const LIVE_POLL_MS = 2000;
/** Steps shown per agent before "Show all steps". */
const STEPS_SHOWN = 10;

// =================================================================================================
// Data: the whole trace for the round views, and the event log's pages (server filters)
// =================================================================================================

export interface TraceLoad {
  state: TraceState;
  error: ApiError | null;
  retry: () => void;
}

/**
 * The run's whole trace, loaded in pages of up to 2,000 events (cursor: next_after_seq) for the
 * round views; a live run is then polled for the events after the last one. Nothing is rendered
 * per event here: the event log pages separately.
 */
function useFullTrace(runId: string): TraceLoad {
  const [state, setState] = useState<TraceState>(EMPTY_TRACE);
  const [error, setError] = useState<ApiError | null>(null);
  const [attempt, setAttempt] = useState(0);
  const stateRef = useRef<TraceState>(EMPTY_TRACE);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = async (): Promise<void> => {
      for (;;) {
        if (cancelled) return;
        let page: dto.TimelineView;
        try {
          page = await api.timeline(runId, { after_seq: stateRef.current.cursor, limit: TRACE_PAGE_SIZE });
        } catch (err) {
          if (cancelled) return;
          const failure = toApiError(err);
          setError(failure);
          // The API being down is shown once by the shell; this view retries by itself.
          if (failure.offline) timer = window.setTimeout(() => void load(), 5000);
          return;
        }
        if (cancelled) return;
        const next = addTracePage(stateRef.current, page);
        stateRef.current = next;
        setState(next);
        setError(null);
        const what = traceNext(next);
        if (what === "page") continue;
        if (what === "poll") timer = window.setTimeout(() => void load(), LIVE_POLL_MS);
        return;
      }
    };
    void load();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [runId, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { state, error, retry };
}

export interface LogLoad {
  state: LogState;
  error: ApiError | null;
  busy: boolean;
  /** The next page after the last event shown (or, for a live run, any newer events). */
  loadMore: () => void;
}

/** The event log: GET /runs/{id}/timeline with the filters, 100 events a page, "load more" by cursor. */
function useEventLog(runId: string, filters: LogFilters): LogLoad {
  const key = `${runId}|${filters.agent ?? ""}|${filters.round ?? ""}|${filters.kind ?? ""}|${filters.tool ?? ""}`;
  const [held, setHeld] = useState<{ key: string; log: LogState }>({ key, log: EMPTY_LOG });
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(true);
  const [asked, setAsked] = useState(0);
  const current = held.key === key ? held.log : EMPTY_LOG;
  const ref = useRef({ key, log: current, filters });
  ref.current = { key, log: current, filters };

  useEffect(() => {
    let cancelled = false;
    const base = ref.current.key === key ? ref.current.log : EMPTY_LOG;
    setBusy(true);
    api.timeline(runId, logQuery(ref.current.filters, base.cursor)).then(
      (page) => {
        if (cancelled) return;
        setHeld({ key, log: addLogPage(base, page) });
        setError(null);
        setBusy(false);
      },
      (err: unknown) => {
        if (cancelled) return;
        setError(toApiError(err));
        setBusy(false);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [runId, key, asked]);

  const loadMore = useCallback(() => setAsked((n) => n + 1), []);
  return { state: current, error, busy, loadMore };
}

// =================================================================================================
// Small pieces
// =================================================================================================

/** "+0.44 s" from the first event of the trace; "+1 min 4 s" past a minute. */
function fmtOffset(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `+${Math.round(ms)} ms`;
  if (ms < 60_000) return `+${(ms / 1000).toFixed(2)} s`;
  return `+${fmtMs(ms)}`;
}

/** "2,134 in / 72 out", or null when the server reported neither. */
function tokensText(input: number | null, output: number | null): string | null {
  if (input === null && output === null) return null;
  return `${fmtInt(input)} in / ${fmtInt(output)} out`;
}

function NotRecorded({ what = "Not recorded" }: { what?: string }) {
  return <span className="not-recorded">{what}</span>;
}

/** An evidence ID linked to Evidence & Decisions (where its raw observation is). */
function EvLink({ id, runId }: { id: string; runId: string }) {
  return (
    <a className="tl-ev" href={hrefFor("evidence", { run: runId, ev: id })} title="Open this evidence item on Evidence & Decisions">
      <Mono nowrap>{id}</Mono>
    </a>
  );
}

function EvLinks({ ids, runId, empty = null }: { ids: readonly string[]; runId: string; empty?: ReactNode }) {
  if (!ids.length) return <>{empty}</>;
  return (
    <span className="chip-row">
      {ids.map((id) => (
        <EvLink key={id} id={id} runId={runId} />
      ))}
    </span>
  );
}

function LaneTag({ agent }: { agent: string | null }) {
  return (
    <span className={`tl-lane-tag lane-${laneKey(agent)}`}>
      <span className="tl-lane-key" aria-hidden="true" />
      {laneName(agent)}
    </span>
  );
}

/** The parts of a one-line description: text, code, and quoted untrusted text with its author. */
type LinePart = string | { code: string } | { quote: string; mixed?: boolean; author?: string };

interface Line extends Omit<EventLine, "parts"> {
  parts: LinePart[];
}

function Parts({ parts, author }: { parts: readonly LinePart[]; author: TextAuthor }) {
  return (
    <>
      {parts.map((part, i) =>
        typeof part === "string" ? (
          <span key={i}>{part}</span>
        ) : "quote" in part ? (
          <UntrustedText
            key={i}
            text={part.quote}
            author={part.author ?? (part.mixed ? `${author.label} (it may also hold a note RECAP added)` : author.label)}
          />
        ) : (
          <code key={i} className="mono">
            {part.code}
          </code>
        ),
      )}
    </>
  );
}

/**
 * Who wrote an incident's symptoms: the simulator's alerting, or, on a live target (its first event
 * says is_simulated false), the probe RECAP measured the cluster with (incident.metadata.source,
 * e.g. recap-kind-probe). Runs before M5 recorded no live target: they all ran on the simulator.
 */
function alertAuthor(p: StatePayload): string {
  const source = str(obj(p.incident?.metadata).source);
  if (p.environment?.is_simulated === false) return `the alert (measured on the live target${source ? ` by ${source}` : ""})`;
  return "the alert (simulated alerting)";
}

/**
 * One event as a line. describeEvent (format.ts) words every category; two lines it would show as
 * RECAP's own words are taken apart here: the alert's symptoms (the alert wrote them) and a
 * lifecycle reason that names the diagnosis (it repeats a claim an agent wrote).
 */
function lineOf(event: TraceEvent, ctx: FeedContext): Line {
  if (event.category === "state.transition") {
    const p = event.payload as StatePayload;
    if (!p.from && p.incident) {
      const symptoms = arr<string>(p.incident.symptoms).filter((s) => typeof s === "string" && s);
      const service = str(p.incident.service);
      return {
        icon: "flag",
        tone: "neutral",
        tag: "STATE",
        parts: [
          "Incident created",
          ...(service ? [" — alert on ", { code: service }] : []),
          ...(symptoms.length ? [" · symptoms ", { quote: symptoms.join("; "), author: alertAuthor(p) }] : []),
        ],
      };
    }
    const diagnosis = diagnosisReason(str(p.reason));
    if (diagnosis) {
      return {
        icon: "arrowRight",
        tone: stateTone(str(p.to)),
        tag: "STATE",
        parts: [{ code: `${str(p.from)} → ${str(p.to, "?")}` }, " · diagnosis: ", { quote: diagnosis.claim }],
      };
    }
  }
  return describeEvent(event, ctx);
}

function LineView({ line, author }: { line: Line; author: TextAuthor }) {
  return (
    <span className="tl-line">
      <Parts parts={line.parts} author={author} />
      {line.chips?.map((chip, i) => (
        <Chip key={`c${i}`} tone={chip.tone} mono>
          {chip.label}
        </Chip>
      ))}
    </span>
  );
}

// =================================================================================================
// The run: where its decisions came from, how it ended, the server's notes
// =================================================================================================

interface RunFactsProps {
  summary: dto.RunSummary | null;
  record: RunRecord | null;
  view: dto.TimelineView;
  entries: readonly dto.TimelineEntry[];
  replay: boolean;
  pace: number | null;
}

/** The tone of how a run ended (RunOutcome): its state token already shows a resolved or running run. */
function runOutcomeTone(outcome: dto.RunOutcome): Tone {
  switch (outcome) {
    case "resolved":
      return "ok";
    case "in_progress":
      return "accent";
    case "budget_exhausted":
    case "quota_deferred":
    case "approval_expired":
      return "warn";
    default:
      return "bad";
  }
}

function sourceOf(summary: dto.RunSummary | null, record: RunRecord | null) {
  if (summary) return summarySource(summary);
  return { provider: record?.provider_info ?? null, environment: record ? (record.environment ?? null) : null };
}

/** The run's own target: its record's environment (name, namespace), else the summary's kind. */
function runTargetOf(summary: dto.RunSummary | null, record: RunRecord | null): TargetFields | null {
  return record?.environment ?? summaryTarget(summary);
}

/** Where the run's tools read and its fix wrote: "Kind cluster recap-m5, namespace recap-demo" and a "live · kind" token. */
function TargetFact({ target }: { target: TargetFacts }) {
  if (target.state === "unknown") return <NotRecorded />;
  const place = targetPlace(target);
  return (
    <span className="tl-target">
      {place ? <span>{place}</span> : null}
      <StatusToken
        size="sm"
        tone={target.state === "live" ? "bad" : "warn"}
        icon={target.state === "live" ? "alert" : "flask"}
        label={target.state === "live" ? `live · ${target.kind || "kind not reported"}` : "simulated"}
        title={target.description || undefined}
      />
    </span>
  );
}

function RunFacts({ summary, record, view, entries, replay, pace }: RunFactsProps) {
  const source = sourceOf(summary, record);
  const target = targetFacts(runTargetOf(summary, record));
  const provenance = summary?.replay ?? null;
  const counts = view.counts ?? {};
  const totals = requestTotals(entries);
  const calls = entries.filter((e) => e.kind === "tool_call");
  const local = calls.filter((e) => isRunbookSearch(e.tool)).length;
  const failedCalls = entries.filter((e) => e.kind === "tool_result" && e.ok === false).length;
  const parallel = view.rounds.filter((r) => r.mode === "parallel").length;
  const escalated = view.rounds.filter((r) => r.escalation === true).length;
  const overlap = bestOverlap(view.rounds);
  const scripted = summary?.provider_class === "scripted" || (!summary && record?.provider_info?.origin === "scripted");
  const agents = [...new Set(view.lanes.map((l) => l.agent))].sort((a, b) => laneRank(a) - laneRank(b));
  const oracle = summary?.oracle ?? null;
  const requests = counts.model_request ?? 0;

  let requestNote: string;
  if (!requests) requestNote = scripted ? "none: the scripted policy is not a model" : "none recorded";
  else if (replay) requestNote = `answered from the recording${totals.retries ? ` · ${totals.retries} retried` : ""}`;
  else requestNote = `${totals.retries} retried · ${totals.failed} unusable`;

  return (
    <Card
      title="This run"
      subtitle="Where its decisions came from, what it ran against, and how it ended. Every figure on this page comes from the run's stored trace."
    >
      <div className="tl-run-head">
        <DataSourceBadge provider={source.provider} environment={source.environment} label="This run's data source" />
        <StateToken state={view.state ?? summary?.state ?? null} />
        {summary && summary.outcome !== "resolved" && summary.outcome !== "in_progress" ? (
          <StatusToken tone={runOutcomeTone(summary.outcome)} label={humanize(summary.outcome)} title="How the run ended, from its recorded state (the server's word)" />
        ) : null}
        {view.live ? <StatusToken tone="accent" icon="clock" label="live: updating" title="This server is running it: new events load every 2 s" /> : null}
      </div>
      <dl className="tl-facts">
        <div>
          <dt>Scenario</dt>
          <dd>
            <Mono>{summary?.scenario_id ?? record?.scenario_id ?? "—"}</Mono>
          </dd>
        </div>
        <div>
          <dt>Model</dt>
          <dd>{summary?.model ? <Mono>{summary.model}</Mono> : scripted ? "none (scripted policy)" : <NotRecorded />}</dd>
        </div>
        <div>
          <dt>Prompt version</dt>
          <dd>{summary?.prompt_version ? <Mono>{summary.prompt_version}</Mono> : <NotRecorded />}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>{summary ? fmtDateTime(summary.started_at) : <NotRecorded />}</dd>
        </div>
        <div>
          <dt>Wall time</dt>
          <dd>
            {summary?.wall_seconds !== null && summary?.wall_seconds !== undefined ? fmtMs(summary.wall_seconds * 1000) : <NotRecorded what={view.live ? "still running" : "Not recorded"} />}
          </dd>
        </div>
        <div>
          <dt>Run ID</dt>
          <dd>
            <Mono>{view.run_id}</Mono>
          </dd>
        </div>
        <div>
          <dt>Target</dt>
          <dd>
            <TargetFact target={target} />
          </dd>
        </div>
      </dl>
      {provenance ? (
        <div className="tl-provenance">
          <Icon name="rewind" size={14} />
          <span>
            Replays recording <Mono>{provenance.recording_id ?? "not recorded"}</Mono>
            {provenance.model ? (
              <>
                {" "}
                of <Mono>{provenance.model}</Mono>
              </>
            ) : null}
            {provenance.original_provider ? ` via ${provenance.original_provider}` : ""}
            {provenance.recorded_at ? `, recorded ${fmtDateTime(provenance.recorded_at)}` : ""}
            {provenance.fixture_title ? (
              <>
                {" "}
                · fixture <q>{provenance.fixture_title}</q>
              </>
            ) : null}
            {provenance.recording_matches_fixture === false ? " · the recording does not match its manifest entry" : ""}
            {provenance.originally_approved_by ? ` · the original fix was approved by ${provenance.originally_approved_by}` : ""}
            {pace ? ` · demo pacing: this replay waited ${Math.round(pace * 100)}% of each recorded answer's time (at most 15 s each)` : ""}
          </span>
        </div>
      ) : null}
      <div className="stat-grid compact tl-stats">
        <StatTile label="Trace events" value={fmtInt(view.total_events)} note={`${Object.keys(counts).length} kinds · all in the event log`} />
        <StatTile
          label="Rounds"
          value={fmtInt(view.rounds.length)}
          tone={parallel ? "accent" : "neutral"}
          note={view.rounds.length ? `${parallel} parallel · ${escalated} escalated` : "none recorded"}
        />
        <StatTile label="Agent turns" value={fmtInt(view.lanes.length)} note={agents.length ? agents.map(laneName).join(", ") : "none recorded"} />
        <StatTile
          label="Tool calls"
          value={fmtInt(counts.tool_call ?? 0)}
          note={`${failedCalls} failed${local ? ` · ${local} runbook search${local === 1 ? "" : "es"} (RECAP's index)` : ""}`}
        />
        <StatTile label={replay ? "Model answers replayed" : "Model requests"} value={fmtInt(requests)} note={requestNote} tone={replay ? "warn" : "neutral"} />
        <StatTile
          label="Turns overlapped"
          value={overlap !== null && overlap > 0 ? fmtMs(overlap) : "—"}
          tone={overlap ? "info" : "neutral"}
          note={overlap === null ? "no parallel round recorded" : "longest measured overlap · not concurrent inference"}
        />
        <StatTile
          label="Fixes sent"
          value={fmtInt(view.retries.fix_attempts)}
          tone={oracle ? (oracle.verified ? "ok" : "bad") : "neutral"}
          note={oracle ? (oracle.verified ? "the oracle verified recovery" : "the oracle saw no recovery") : "no oracle verdict recorded"}
        />
      </div>
      {view.notes.length || view.not_recorded.length ? (
        <div className="tl-server-notes">
          {view.notes.length ? (
            <Notice tone={replay ? "warn" : "info"} icon={replay ? "rewind" : "info"} title="How to read the times in this trace (the server's notes)">
              <ul className="tl-note-list">
                {view.notes.map((note, i) => (
                  <li key={i}>{note}</li>
                ))}
              </ul>
            </Notice>
          ) : null}
          {view.not_recorded.length ? (
            <Notice tone="neutral" icon="minus" title="Not recorded in this trace">
              {view.not_recorded.join(" · ")}
            </Notice>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

// =================================================================================================
// Findings: what each agent concluded, and how the verification judged it (src/view/findings.ts)
// =================================================================================================

/** What every part of a round needs to label text honestly and link evidence. */
interface ViewContext {
  runId: string;
  author: TextAuthor;
  /** The run has ended, so record.verification is the final verification. */
  final: boolean;
  /** More than one agent took part: evidence chips say which agent recorded each item. */
  multiAgent: boolean;
  /** Runbook chunks from a red-team knowledge overlay (they may be deliberately wrong). */
  overlayIds: ReadonlySet<string>;
  /** Model requests were answered from a recording (their times are the original run's). */
  replay: boolean;
  /** The first event's time: offsets count from it. */
  start: number | null;
  /** Describing events: RECAP's notes on each agent's model requests, red-team overlays. */
  feed: FeedContext;
  /** Where the run's tools read and its fix wrote: the simulated shop, a live cluster, or not recorded. */
  target: TargetFacts;
}

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
  disagreement: "the last round left a conflict no evidence rule settled",
  remaining_tool_calls: "tool-call budget left",
  remaining_steps: "step budget left",
  max_parallel_agents: "parallelism cap",
};

function signalValue(value: unknown) {
  if (typeof value === "boolean") return <Chip tone={value ? "accent" : "neutral"}>{value ? "yes" : "no"}</Chip>;
  if (typeof value === "number") return <span className="num">{Number.isInteger(value) ? fmtInt(value) : fmtScore(value)}</span>;
  if (Array.isArray(value))
    return value.length ? (
      <span className="chip-row">
        {value.map((v, i) => (
          <Chip key={i}>{str(v)}</Chip>
        ))}
      </span>
    ) : (
      <span className="muted">none</span>
    );
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
            ? "Parallel round — the agents' turns overlapped"
            : "Parallel round, but the agents did not overlap"}
      </div>
      {/* The overlap is measured. "Back to back" never ran (it is the sum of the agents' times), so it is labeled an estimate. */}
      <div className="muted">
        wall clock {fmtMs(wall)} ·{" "}
        {overlapped
          ? `turns overlapped for ${fmtMs(overlap)} (running them back to back would have taken about ${fmtMs(sum)} — an estimate)`
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

/** The router's view of a round: the signals it used, the step allocation and how the agents ran. */
function RoutingDetails({ event, orchestration }: { event: TraceEvent; orchestration: TraceEvent | undefined }) {
  const p = event.payload as RoutingPayload;
  const signals = obj(p.signals);
  const budget = obj(p.budget);
  return (
    <div className="round-grid tl-routing">
      <div>
        <h5 className="mini-title">Signals the router saw</h5>
        <div className="table-wrap">
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
      </div>
      <div className="round-side">
        <h5 className="mini-title">Step allocation</h5>
        {Object.keys(budget).length ? (
          <KeyValues rows={Object.entries(budget).map(([k, v]) => [<Mono key={k}>{k}</Mono>, `${str(v)} steps`])} />
        ) : (
          <span className="muted">—</span>
        )}
        <h5 className="mini-title">Orchestration</h5>
        <OrchestrationView event={orchestration} />
      </div>
    </div>
  );
}

/**
 * The tag on text RECAP did not write: shown as data, never followed and never a verdict.
 * `mixed`: the text may still hold a note RECAP added that could not be told apart.
 */
function UntrustedTag({ author, mixed = false }: { author: TextAuthor; mixed?: boolean }) {
  return mixed ? (
    <span
      className="trust-tag trust-untrusted"
      title={`Written by ${author.label}, and it may also hold a note RECAP added that this page cannot tell apart (a run before M3, or notes its trace does not confirm). Untrusted data: shown as text, never followed.`}
    >
      untrusted · {author.short} text; may include RECAP's notes
    </span>
  ) : (
    <span className="trust-tag trust-untrusted" title={`Written by ${author.label}. Untrusted data: shown as text, never followed.`}>
      untrusted · {author.short}
    </span>
  );
}

/** Untrusted free text, quoted under its tag. React renders it as text (escaped), never as HTML. */
function Said({ text, author, mixed = false }: { text: string; author: TextAuthor; mixed?: boolean }) {
  return (
    <div className="tl-said">
      <UntrustedTag author={author} mixed={mixed} />
      <q className="tl-quote">{text}</q>
    </div>
  );
}

/** A note RECAP added to an agent's text (trusted: RECAP's own words). */
function RecapNote({ children, title }: { children: string; title?: string }) {
  return (
    <div className="tl-recap-note">
      <span className="trust-tag trust-trusted" title={title ?? "Written by RECAP, not by the agent's provider."}>
        RECAP's note
      </span>
      <span>{children}</span>
    </div>
  );
}

/** A red-team overlay chunk: indexed for this run only, and it may be deliberately wrong. */
function OverlayChip() {
  return (
    <Chip tone="warn" title="From an extra knowledge folder indexed for this red-team run: it may be deliberately wrong">
      <Icon name="alert" size={12} /> overlay
    </Chip>
  );
}

/** An evidence ID linked to the Evidence page, with the agent that recorded it. */
function EvidenceChip({ ev, ctx, showAgent = ctx.multiAgent }: { ev: EvidenceRef; ctx: ViewContext; showAgent?: boolean }) {
  return (
    <a className="tl-ev" href={hrefFor("evidence", { run: ctx.runId, ev: ev.id })} title={ev.sourceRef ?? undefined}>
      <Mono nowrap>{ev.id}</Mono>
      {showAgent && ev.agent ? <span className="tl-ev-agent">{shortRole(ev.agent)}</span> : null}
      {ev.known === false ? <span className="tl-ev-missing">not in this run</span> : null}
    </a>
  );
}

function EvidenceRow({ label, refs, ctx }: { label: string; refs: EvidenceRef[]; ctx: ViewContext }) {
  return (
    <div className="tl-ev-row">
      <span className="tl-ev-label">{label}</span>
      {refs.length ? refs.map((ev, i) => <EvidenceChip key={`${ev.id}-${i}`} ev={ev} ctx={ctx} />) : <span className="muted small">none</span>}
    </div>
  );
}

/** What the merger keyed a claim by (its release or blamed dependency), as the verdict judged it. */
function JudgedDetail({ check, mechanism, ctx }: { check: CheckView; mechanism: string; ctx: ViewContext }) {
  if (!check.detail) return null;
  if (!check.detailChecked) {
    // Not shaped like a version or a service: the claim's own text, so it is shown as data.
    return (
      <span className="chip-row">
        <span className="muted small" title="The merger keys a claim by this detail: two releases are judged apart">
          judged as
        </span>
        <Said text={check.detail} author={ctx.author} />
      </span>
    );
  }
  return (
    <Chip mono title="What the merger keys this claim by (resolved from the evidence when the claim names none): two releases or dependencies are judged apart">
      {`judged as ${detailLabel(mechanism, check.detail)}`}
    </Chip>
  );
}

function CheckLine({ check, mechanism, ctx }: { check: CheckView; mechanism: string; ctx: ViewContext }) {
  return (
    <>
      <div className="chip-row">
        <HypothesisChip status={check.status} />
        <JudgedDetail check={check} mechanism={mechanism} ctx={ctx} />
        <span className="muted small">proposed by</span>
        {check.proposers === null ? (
          <span className="muted small">not recorded</span>
        ) : check.proposers.length ? (
          check.proposers.map((role) => (
            <Chip key={role} mono>
              {role}
            </Chip>
          ))
        ) : (
          <span className="muted small">—</span>
        )}
      </div>
      {check.failed.length ? (
        <ul className="rule-list">
          {check.failed.map((line, i) => (
            <li key={i} className="text-bad">
              <Icon name="x" size={13} /> {line}
              {/* RECAP's sentence, repeating a value the claim named that is not a version: model text. */}
              {check.failedQuotesClaim[i] ? (
                <span
                  className="trust-tag trust-untrusted tl-rule-tag"
                  title={`RECAP's rule line, but it repeats a value the claim named, which ${ctx.author.label} wrote: that part is untrusted data.`}
                >
                  quotes {ctx.author.whose} text
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {check.basis?.length ? <EvidenceRow label="its rules relied on" refs={check.basis} ctx={ctx} /> : null}
    </>
  );
}

/** Which verification a claim's verdict comes from (the last one that read it). */
function verdictSource(judged: JudgedBy, verdict: FinalVerdict, ctx: ViewContext): { label: string; title?: string } {
  if (verdict.kind === "unverified" && verdict.reason !== "not_recorded") return { label: "Verification" };
  if (judged.kind === "earlier") {
    return {
      label: `Verification before fix ${judged.fix}`,
      title: `The last verification that read this claim. Fix ${judged.fix} did not recover the system, so RECAP started over: later verifications, the final one included, judged only newer findings.`,
    };
  }
  return { label: ctx.final ? "Final verification" : "Latest verification · run still going" };
}

const UNVERIFIED_NOTES: Record<"partial" | "stopped" | "pending", string> = {
  partial: "partial: round cut short by a budget stop, not verified",
  stopped: "never verified: the run stopped before the verifier read this round",
  pending: "not verified yet: the verifier has not read this round",
};

/** The verification's verdict on one claim, labeled as the final one only once the run has ended. */
function VerdictLine({ verdict, judged, mechanism, ctx }: { verdict: FinalVerdict; judged: JudgedBy; mechanism: string; ctx: ViewContext }) {
  const source = verdictSource(judged, verdict, ctx);
  return (
    <div className="tl-verdict">
      <span className="tl-verdict-label" title={source.title}>
        {source.label}
      </span>
      {verdict.kind === "unverified" ? (
        <span className={verdict.reason === "not_recorded" ? "muted small" : "text-warn small"}>
          {verdict.reason === "not_recorded" ? (ctx.final ? "no verification was recorded" : "not recorded yet") : UNVERIFIED_NOTES[verdict.reason]}
        </span>
      ) : null}
      {verdict.kind === "unchecked" ? (
        <span className="muted small" title="No check in this verification has the claim's mechanism, target and origin.">
          did not check this claim
        </span>
      ) : null}
      {verdict.kind === "matched" ? <CheckLine check={verdict.check} mechanism={mechanism} ctx={ctx} /> : null}
      {verdict.kind === "ambiguous" ? (
        <div className="chip-row">
          <span className="muted small">{verdict.checks.length} checks share this claim's mechanism and target; the record does not say which one judged it:</span>
          {verdict.checks.map((c, i) => (
            <span key={i} className="chip-row">
              <HypothesisChip status={c.status} />
              <JudgedDetail check={c} mechanism={mechanism} ctx={ctx} />
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ClaimCard({ claim, judged, ctx }: { claim: ClaimView; judged: JudgedBy; ctx: ViewContext }) {
  return (
    <div className="tl-claim">
      {claim.claim ? <Said text={claim.claim} author={ctx.author} /> : <span className="muted small">no claim text</span>}
      <div className="chip-row">
        <Mono>{claim.mechanism}</Mono>
        <span className="muted">on</span>
        <Mono>{claim.target ?? "no service named"}</Mono>
        {claim.details.map(([key, value]) =>
          // A release or dependency not shaped like one is the claim's own text: quoted data.
          isCheckedDetail(key, value) ? (
            <Chip key={key} mono>
              {`${key}=${value}`}
            </Chip>
          ) : (
            <span key={key} className="chip-row">
              <span className="muted small">{key}</span>
              <Said text={value} author={ctx.author} />
            </span>
          ),
        )}
        {claim.origin === "memory" ? <Chip tone="info">recalled from memory</Chip> : null}
      </div>
      <EvidenceRow label="cites" refs={claim.supporting} ctx={ctx} />
      {claim.contradicting.length ? <EvidenceRow label="argues against" refs={claim.contradicting} ctx={ctx} /> : null}
      {claim.confidenceNote ? (
        <div className="tl-note">
          {/* The model's own words: quoted and labeled, never shown as a score. */}
          <span className="tl-note-label">Confidence note · {ctx.author.whose} own words, not a score</span>
          <Said text={claim.confidenceNote} author={ctx.author} />
        </div>
      ) : null}
      <VerdictLine verdict={claim.verdict} judged={judged} mechanism={claim.mechanism} ctx={ctx} />
    </div>
  );
}

function SearchStatus({ search }: { search: RunbookSearch }) {
  switch (search.status) {
    case "failed":
      return (
        <>
          <Chip tone="bad">failed</Chip>
          {search.error ? <span className="text-bad small">{search.error}</span> : null}
        </>
      );
    case "running":
      return <Chip tone="accent">in flight</Chip>;
    case "no_result":
      return <span className="muted small">no result recorded</span>;
    default:
      return null;
  }
}

/** One search_runbooks call: RECAP's own index answered it, never the environment's MCP server. */
function SearchView({ search, ctx }: { search: RunbookSearch; ctx: ViewContext }) {
  return (
    <div className="tl-search">
      <div className="chip-row">
        <Chip tone="info">
          <Icon name="book" size={12} /> answered by RECAP's runbook index
        </Chip>
        <span className="muted small">a local read tool, not the environment's MCP server</span>
        <SearchStatus search={search} />
      </div>
      <div className="tl-ev-row">
        <span className="tl-ev-label">query{search.k !== null ? ` (up to ${fmtInt(search.k)} sections)` : ""}</span>
      </div>
      {search.query ? <Said text={search.query} author={ctx.author} /> : <span className="muted small">no query recorded</span>}
      {search.hits === null ? (
        <span className="muted small">results not recorded</span>
      ) : search.hits.length ? (
        <>
          <span className="tl-ev-label">sections found (headings are runbook text: untrusted)</span>
          <ol className="tl-hits">
            {search.hits.map((hit, i) => (
              <li key={`${hit.chunkId}-${i}`}>
                <span className="tl-hit-rank">#{hit.rank}</span>
                <Mono>{hit.chunkId}</Mono>
                {ctx.overlayIds.has(hit.chunkId) ? <OverlayChip /> : null}
                {hit.heading ? <span className="muted">{hit.heading}</span> : null}
                {hit.signals.length ? <span className="tl-signals">{hit.signals.map((s) => `${s.label} ${s.value}`).join(" · ")}</span> : null}
              </li>
            ))}
          </ol>
        </>
      ) : (
        <span className="muted small">no sections found</span>
      )}
      {search.evidenceId ? (
        <div className="tl-ev-row">
          <span className="tl-ev-label">receipt</span>
          <EvidenceChip ev={{ id: search.evidenceId, agent: null, sourceRef: null, sourceType: "doc", flags: [], known: null }} ctx={ctx} />
          <span className="muted small">DOC evidence: the verifier never counts it as proof</span>
        </div>
      ) : null}
    </div>
  );
}

function sameQuestions(a: string[] | null, b: string[] | null): boolean {
  return !!a && !!b && a.length === b.length && a.every((q, i) => q === b[i]);
}

/** "1 step", "3 steps"; "— steps" when not recorded. */
function counted(n: number | null, one: string): string {
  return `${fmtInt(n)} ${n === 1 ? one : `${one}s`}`;
}

// =================================================================================================
// One agent's steps: context pack -> model request -> decision -> tool calls (with verdicts)
// =================================================================================================

function ContextLine({ entry, ctx }: { entry: dto.TimelineEntry; ctx: ViewContext }) {
  const pack = contextPack(entry);
  const [open, setOpen] = useState(false);
  const used = pack.tokensAfter;
  return (
    <div className="tl-sline">
      <span className="tl-sline-icon" aria-hidden="true">
        <Icon name="package" size={13} />
      </span>
      <div className="tl-sline-body">
        <div className="tl-sline-head">
          <span className="tl-sline-what">Context pack</span>
          {used !== null && pack.budget !== null ? (
            <Meter value={used} max={pack.budget} width={72} label={`${fmtInt(used)} / ${fmtInt(pack.budget)} tokens`} />
          ) : (
            <NotRecorded what="tokens not recorded" />
          )}
          {pack.estimate ? (
            <span className="muted small" title="RECAP counts these tokens itself before packing the context; no model reported them">
              RECAP's estimate
            </span>
          ) : null}
          {pack.dropped.length ? (
            <Chip tone="warn" title="Items left out to stay inside the context budget">
              {pack.dropped.length} dropped
            </Chip>
          ) : null}
          {pack.items.length ? (
            <button type="button" className="tl-link-btn" aria-expanded={open} onClick={() => setOpen(!open)}>
              {pack.items.length} item{pack.items.length === 1 ? "" : "s"}
              <Icon name={open ? "chevronDown" : "chevronRight"} size={12} />
            </button>
          ) : null}
        </div>
        {open ? (
          <div className="tl-items">
            {pack.items.map((id) =>
              itemKind(id) === "evidence" ? (
                <EvLink key={id} id={id} runId={ctx.runId} />
              ) : (
                <Chip key={id} mono tone={ctx.overlayIds.has(id) ? "warn" : "neutral"} title={itemKind(id) === "runbook" ? "A runbook section (untrusted document text)" : undefined}>
                  {id}
                </Chip>
              ),
            )}
            {pack.dropped.length ? (
              <span className="muted small">
                dropped: <Mono>{pack.dropped.join(", ")}</Mono>
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** One model request: tokens as the server reported them, and where its time went. */
function RequestLine({ mr, ctx, label = "Model request" }: { mr: dto.ModelRequest; ctx: ViewContext; label?: string }) {
  const split = requestSplit(mr);
  const tokens = tokensText(mr.input_tokens, mr.output_tokens);
  return (
    <div className={mr.ok ? "tl-sline" : "tl-sline tl-sline-bad"}>
      <span className="tl-sline-icon" aria-hidden="true">
        <Icon name="zap" size={13} />
      </span>
      <div className="tl-sline-body">
        <div className="tl-sline-head">
          <span className="tl-sline-what">{label}</span>
          <strong className="tl-num">{fmtMs(split.totalMs)}</strong>
          <RequestBar split={split} />
          {mr.replayed ? (
            <Chip tone="warn" title="Answered from a recording: its time, server time and tokens are the original run's; no model was called now">
              <Icon name="rewind" size={11} /> original run's time
            </Chip>
          ) : null}
          {mr.retry ? <Chip title="A corrective retry after an unusable answer">attempt {mr.attempt}</Chip> : null}
          {!mr.ok ? (
            <Chip tone="bad">
              <Icon name="x" size={11} /> unusable answer
            </Chip>
          ) : null}
        </div>
        <div className="tl-sline-meta">
          <span>
            <Mono>{mr.model_reported || mr.model}</Mono> · {mr.purpose || "—"} · {mr.mode || "—"}
            {mr.response_kind ? ` · answered ${humanize(mr.response_kind)}` : ""}
          </span>
          <span>tokens {tokens ?? <NotRecorded what="not reported by the server" />}</span>
          {split.serverMs !== null ? (
            <span>
              server {fmtMs(split.serverMs)}
              {split.computeMs !== null && split.waitMs !== null ? (
                <>
                  {" "}
                  = compute {fmtMs(split.computeMs)} + <span className={split.waitMs >= 1000 ? "tl-wait" : undefined}>waiting inside the server {fmtMs(split.waitMs)}</span>
                </>
              ) : (
                " (compute not reported)"
              )}
              {split.outsideMs !== null ? ` · outside the server ${fmtMs(split.outsideMs)}` : ""}
            </span>
          ) : (
            <span>
              server time <NotRecorded what="not reported" />
            </span>
          )}
          {ctx.replay && !mr.replayed ? <span className="text-warn">not from the recording</span> : null}
        </div>
      </div>
    </div>
  );
}

/** The agent's decision for a step (RECAP's reading of the provider's answer). */
function DecisionLine({ entry }: { entry: dto.TimelineEntry }) {
  const p = obj(entry.payload);
  const action = str(p.action, "?");
  const calls = arr<{ tool_name?: unknown }>(p.tool_calls).map((c) => str(c.tool_name, "?"));
  const claims = arr(p.hypotheses).length;
  const counts = new Map<string, number>();
  for (const name of calls) counts.set(name, (counts.get(name) ?? 0) + 1);
  return (
    <div className="tl-sline">
      <span className="tl-sline-icon" aria-hidden="true">
        <Icon name="cpu" size={13} />
      </span>
      <div className="tl-sline-body">
        <div className="tl-sline-head">
          <span className="tl-sline-what">Decided</span>
          {action === "call_tools" ? (
            <span>
              to call{" "}
              {[...counts].map(([name, n], i) => (
                <span key={name}>
                  {i ? ", " : ""}
                  <Mono>{name}</Mono>
                  {n > 1 ? ` ×${n}` : ""}
                </span>
              ))}
            </span>
          ) : action === "finish" ? (
            <span>to finish{claims ? ` with ${claims} claim${claims === 1 ? "" : "s"} for the verifier` : ""}</span>
          ) : (
            <Mono>{humanize(action)}</Mono>
          )}
        </div>
      </div>
    </div>
  );
}

function PolicyChip({ entry }: { entry: dto.TimelineEntry }) {
  const p = entry.payload as PolicyPayload;
  const verdict = str(p.verdict, entry.verdict ?? "?");
  const rules = arr<string>(p.rule_ids);
  const deciding = rules[rules.length - 1];
  return (
    <StatusToken
      size="sm"
      tone={verdictTone(verdict)}
      icon={verdict === "ALLOW" ? "check" : verdict === "DENY" ? "x" : "key"}
      label={verdictLabel(verdict)}
      detail={deciding}
      title={`The gateway's policy verdict${deciding ? `; the deciding rule is ${deciding}` : ""}. ${arr<string>(p.reasons).join("; ")}`}
    />
  );
}

/**
 * Where a tool call was answered: RECAP's runbook index, or the environment's MCP server; on a live
 * target the server read the real cluster (its namespace named), so the chip says so.
 */
function ToolSourceChip({ local, target }: { local: boolean; target: TargetFacts }) {
  if (local) {
    return (
      <Chip tone="info" title="search_runbooks is answered by RECAP's own runbook index, not the environment's MCP server">
        RECAP's index
      </Chip>
    );
  }
  if (target.state === "live") {
    const { place, real } = liveTargetWords(target);
    return (
      <Chip
        outline
        tone="bad"
        title={`A read-only tool on RECAP's MCP server, answered live from ${place} (${real}). Investigators may only call read tools (R7-worker-read-only).`}
      >
        MCP · live{target.namespace ? ` · ${target.namespace}` : ""}
      </Chip>
    );
  }
  return (
    <Chip outline title="A read-only tool on the environment's MCP server">
      MCP
    </Chip>
  );
}

/** One tool call: the call, the gateway's verdict, and its result (with the evidence it recorded). */
function ToolLine({ tool, ctx }: { tool: ToolStep; ctx: ViewContext }) {
  const call = tool.call ? obj(tool.call.payload) : {};
  const args = obj(call.args);
  const local = isRunbookSearch(tool.tool);
  const { query, ...rest } = args;
  const free = local && typeof query === "string" ? query : "";
  const result = tool.result ? obj(tool.result.payload) : null;
  const ok = tool.result?.ok;
  const denied = tool.policy && str(obj(tool.policy.payload).verdict) !== "ALLOW";
  const flags = result ? [...arr<string>(result.flags), ...(result.truncated === true && !arr(result.flags).includes("truncated") ? ["truncated"] : [])] : [];
  return (
    <div className={ok === false || denied ? "tl-sline tl-sline-bad" : "tl-sline"}>
      <span className="tl-sline-icon" aria-hidden="true">
        <Icon name={local ? "book" : "tool"} size={13} />
      </span>
      <div className="tl-sline-body">
        <div className="tl-sline-head">
          <code className="mono tl-call">{local ? tool.tool : `${tool.tool}(${fmtArgs(tool.call ? args : {})})`}</code>
          <ToolSourceChip local={local} target={ctx.target} />
          {tool.policy ? <PolicyChip entry={tool.policy} /> : null}
        </div>
        {free ? (
          <div className="tl-sline-meta">
            <span>
              query <UntrustedText text={free} author={ctx.author.label} />
              {Object.keys(rest).length ? (
                <>
                  {" "}
                  · <Mono>{fmtArgs(rest)}</Mono>
                </>
              ) : null}
            </span>
          </div>
        ) : null}
        <div className="tl-sline-meta">
          {tool.result ? (
            ok === false ? (
              <span className="text-bad">
                <Icon name="x" size={12} /> failed: {str(result?.error, "no error text recorded")}
              </span>
            ) : (
              <span className="tl-result">
                <Icon name="check" size={12} /> result
                {tool.result.evidence_ids.length ? (
                  <>
                    {" "}
                    → <EvLinks ids={tool.result.evidence_ids} runId={ctx.runId} />
                  </>
                ) : (
                  " (no evidence item)"
                )}
                {" · "}
                {fmtMs(num(result?.duration_ms) ?? tool.result.duration_ms)}
              </span>
            )
          ) : tool.call ? (
            <span className="muted">{denied ? "" : "no result recorded"}</span>
          ) : denied ? (
            <span className="text-bad">refused by the gateway: the call was never sent</span>
          ) : (
            <span className="muted">no call recorded</span>
          )}
          {flags.map((flag) => (
            <FlagChip key={flag} flag={flag} />
          ))}
        </div>
      </div>
    </div>
  );
}

function OtherLine({ entry, ctx }: { entry: dto.TimelineEntry; ctx: ViewContext }) {
  if (entry.kind === "injection_flag") {
    const p = entry.payload as InjectionPayload;
    return (
      <div className="tl-sline tl-sline-warn">
        <span className="tl-sline-icon" aria-hidden="true">
          <Icon name="alert" size={13} />
        </span>
        <div className="tl-sline-body">
          <div className="tl-sline-head">
            <span className="tl-sline-what">Injection-like text flagged</span>
            {p.evidence_id ? <EvLink id={p.evidence_id} runId={ctx.runId} /> : null}
            {arr<string>(p.patterns).map((pattern) => (
              <Chip key={pattern} tone="warn" mono>
                {pattern}
              </Chip>
            ))}
          </div>
          {p.excerpt ? (
            <div className="tl-sline-meta">
              <span>
                excerpt <UntrustedText text={p.excerpt} author={`a tool's output (${str(p.tool, "tool")})`} />
              </span>
              <span className="muted">kept as untrusted data: RECAP never follows it</span>
            </div>
          ) : null}
        </div>
      </div>
    );
  }
  return (
    <div className="tl-sline">
      <span className="tl-sline-icon" aria-hidden="true">
        <Icon name="dot" size={13} />
      </span>
      <div className="tl-sline-body">
        <LineView line={lineOf(toTraceEvent(entry, ctx.runId), ctx.feed)} author={ctx.author} />
      </div>
    </div>
  );
}

function StepItem({ step, index, ctx }: { step: AgentStep; index: number; ctx: ViewContext }) {
  const first = step.context ?? step.requests[0] ?? step.decision ?? step.tools[0]?.policy ?? step.tools[0]?.call ?? step.other[0] ?? null;
  return (
    <li className="tl-step">
      <div className="tl-step-head">
        <span className="tl-step-no">Step {step.step ?? index + 1}</span>
        {first ? <span className="tl-step-time">{fmtOffset(offsetOf(first, ctx.start))}</span> : null}
      </div>
      {step.context ? <ContextLine entry={step.context} ctx={ctx} /> : null}
      {step.requests.map((entry) => (entry.model_request ? <RequestLine key={entry.seq} mr={entry.model_request} ctx={ctx} /> : null))}
      {step.decision ? <DecisionLine entry={step.decision} /> : null}
      {step.tools.map((tool) => (
        <ToolLine key={tool.key} tool={tool} ctx={ctx} />
      ))}
      {step.other.map((entry) => (
        <OtherLine key={entry.seq} entry={entry} ctx={ctx} />
      ))}
    </li>
  );
}

function StepList({ turn, ctx }: { turn: AgentTurn; ctx: ViewContext }) {
  const [all, setAll] = useState(false);
  const requests = turn.steps.reduce((n, s) => n + s.requests.length, 0);
  const calls = turnToolCalls(turn);
  const shown = all ? turn.steps : turn.steps.slice(0, STEPS_SHOWN);
  return (
    <details className="tl-steps" open>
      <summary>
        <Icon name="chevronRight" size={13} />
        <span className="tl-steps-title">Steps</span>
        <span className="muted small">
          {counted(turn.steps.length, "step")} · {counted(requests, ctx.replay ? "replayed model answer" : "model request")} · {counted(calls, "tool call")}
        </span>
      </summary>
      {turn.steps.length ? (
        <ol className="tl-step-list">
          {shown.map((step, i) => (
            <StepItem key={step.key} step={step} index={i} ctx={ctx} />
          ))}
        </ol>
      ) : (
        <span className="muted small">No steps recorded for this turn.</span>
      )}
      {turn.steps.length > STEPS_SHOWN && !all ? (
        <button type="button" className="btn btn-small" onClick={() => setAll(true)}>
          Show all {turn.steps.length} steps
        </button>
      ) : null}
    </details>
  );
}

/** The lane's figures: turn time (this run's), model requests and their recorded time and tokens. */
function LaneFacts({ lane, totals, ctx, scripted }: { lane: dto.AgentLane | null; totals: RequestTotals; ctx: ViewContext; scripted: boolean }) {
  const tokens = tokensText(totals.inputTokens, totals.outputTokens);
  return (
    <dl className="tl-lane-facts">
      <div>
        <dt>Turn</dt>
        <dd>
          {lane?.duration_ms !== null && lane?.duration_ms !== undefined ? (
            <>
              {fmtMs(lane.duration_ms)}
              {ctx.replay ? <span className="muted"> (this replay)</span> : null}
            </>
          ) : (
            <NotRecorded what={lane?.outcome === "running" ? "still running" : "not finished"} />
          )}
        </dd>
      </div>
      <div>
        <dt>{ctx.replay ? "Model answers" : "Model requests"}</dt>
        <dd>
          {totals.count ? (
            <>
              {fmtInt(totals.count)}
              {totals.retries ? <span className="muted"> ({totals.retries} retried)</span> : null}
            </>
          ) : scripted ? (
            "none (scripted policy)"
          ) : (
            <NotRecorded what="none recorded" />
          )}
        </dd>
      </div>
      {totals.count ? (
        <>
          <div>
            <dt>Tokens</dt>
            <dd>
              {tokens ?? <NotRecorded what="not reported" />}
              {tokens && totals.unreported ? <span className="muted"> ({totals.unreported} without counts)</span> : null}
            </dd>
          </div>
          <div>
            <dt>Request time</dt>
            <dd>
              {fmtMs(totals.durationMs)}
              {ctx.replay ? <span className="muted"> (original run)</span> : null}
              {totals.waitMs !== null && totals.waitMs > 0 ? <span className="tl-wait"> · {fmtMs(totals.waitMs)} waiting in the server</span> : null}
            </dd>
          </div>
        </>
      ) : null}
    </dl>
  );
}

function AgentColumn({
  agent,
  round,
  lane,
  turn,
  roundEntries,
  ctx,
  scripted,
}: {
  agent: AgentView;
  round: RoundView;
  lane: dto.AgentLane | null;
  turn: AgentTurn | undefined;
  roundEntries: readonly dto.TimelineEntry[];
  ctx: ViewContext;
  scripted: boolean;
}) {
  const ownQuestions = agent.questions?.length && !sameQuestions(agent.questions, round.questions) ? agent.questions : null;
  // The provider's words and the notes RECAP added to them (format.ts splitSummary).
  const ended = agent.status === "finished" ? agent.outcome.key : "";
  const summary = splitSummary(agent.summary, ended, agent.modelNotes);
  const totals = useMemo(() => requestTotals(roundEntries.filter((e) => e.agent === agent.role)), [roundEntries, agent.role]);
  return (
    <div className={agent.partial ? `tl-agent tl-partial lane-${laneKey(agent.role)}` : `tl-agent lane-${laneKey(agent.role)}`}>
      <div className="tl-agent-head">
        <span className="tl-lane-key" aria-hidden="true" />
        <h4 className="tl-agent-name">{roleLabel(agent.role)}</h4>
        <Chip tone={agent.outcome.tone} dot title={agent.outcome.key ? `outcome: ${agent.outcome.key}` : undefined}>
          {agent.outcome.label}
        </Chip>
        {agent.partial ? <Chip tone="warn">partial</Chip> : null}
        <span className="tl-agent-stats">
          {counted(agent.steps, "step")} · {counted(agent.toolCalls, "tool call")}
        </span>
      </div>
      <LaneFacts lane={lane} totals={totals} ctx={ctx} scripted={scripted} />
      {agent.outcome.note ? <div className="tl-outcome-note">{agent.outcome.note}</div> : null}
      {agent.partial ? (
        <div
          className="tl-partial-note"
          title="AgentFinding.partial: this finding was kept when a budget stop cut its round short. No verification read it, and it never reaches memory."
        >
          <Icon name="alert" size={13} /> partial: round cut short by a budget stop, not verified
        </div>
      ) : null}

      {ownQuestions ? (
        <Notice tone="info" icon="search" title="The verifier asked this agent (RECAP's own text)">
          <ul className="tl-questions">
            {ownQuestions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        </Notice>
      ) : null}

      {turn ? <StepList turn={turn} ctx={ctx} /> : <span className="muted small">No steps of this agent are in the loaded trace.</span>}

      {agent.claims.length ? (
        <div className="tl-section">
          <h5 className="mini-title">Its claims ({agent.claims.length})</h5>
          <div className="tl-claims">
            {agent.claims.map((claim, i) => (
              <ClaimCard key={i} claim={claim} judged={round.judgedBy} ctx={ctx} />
            ))}
          </div>
        </div>
      ) : agent.claimTexts.length ? (
        <div className="tl-section">
          <h5 className="mini-title">Its claims (from agent.finish)</h5>
          {agent.claimTexts.map((claim, i) => (
            <Said key={i} text={claim} author={ctx.author} />
          ))}
          <span className="muted small">Verdicts appear once the run record has this finding.</span>
        </div>
      ) : agent.status === "finished" ? (
        <span className="muted small">No claims.</span>
      ) : null}

      {agent.dropped?.length ? (
        <div className="tl-dropped">
          {agent.dropped.map((d) => (
            <div className="tl-dropped-line" key={d.mechanism}>
              <Chip tone="warn">dropped</Chip>
              <span>{droppedLine(agent.role, d)}</span>
            </div>
          ))}
          <span className="muted small">RECAP removed these claims before the verifier saw them: a role's contract lists the mechanisms it may conclude.</span>
        </div>
      ) : null}

      <div className="tl-section">
        <h5 className="mini-title">Evidence it recorded{agent.evidence.length ? ` (${agent.evidence.length})` : ""}</h5>
        {agent.evidence.length ? (
          <ul className="tl-ev-list">
            {agent.evidence.map((ev) => (
              <li key={ev.id}>
                <EvidenceChip ev={ev} ctx={ctx} showAgent={false} />
                {ev.sourceRef ? (
                  <code className="mono truncate" title={ev.sourceRef}>
                    {ev.sourceRef}
                  </code>
                ) : null}
                {ev.sourceType === "doc" ? <Chip title="DOC evidence: the verifier never counts it as proof">doc</Chip> : null}
                {ev.flags.map((flag) => (
                  <FlagChip key={flag} flag={flag} />
                ))}
              </li>
            ))}
          </ul>
        ) : (
          <span className="muted small">{agent.status === "running" ? "none yet" : "none in this round"}</span>
        )}
      </div>

      {agent.searches.length ? (
        <div className="tl-section">
          <h5 className="mini-title">Runbook searches ({agent.searches.length})</h5>
          {agent.searches.map((search) => (
            <SearchView key={search.id} search={search} ctx={ctx} />
          ))}
        </div>
      ) : null}

      {agent.hasFinding ? (
        <div className="tl-section">
          <h5 className="mini-title">Citations: runbook chunks it saw</h5>
          {agent.docRefs === null ? (
            <span className="muted small">not recorded</span>
          ) : agent.docRefs.length ? (
            <span className="chip-row">
              {agent.docRefs.map((id) =>
                ctx.overlayIds.has(id) ? (
                  <span key={id} className="chip-row">
                    <Chip mono tone="warn">
                      {id}
                    </Chip>
                    <OverlayChip />
                  </span>
                ) : (
                  <Chip key={id} mono>
                    {id}
                  </Chip>
                ),
              )}
            </span>
          ) : (
            <span className="muted small">none</span>
          )}
        </div>
      ) : null}

      {summary.said || summary.notes.length ? (
        <div className="tl-section">
          <h5 className="mini-title">Its summary</h5>
          {summary.said ? <Said text={summary.said} author={ctx.author} mixed={!summary.sure} /> : null}
          {summary.notes.map((note, i) => (
            <RecapNote key={i} title="Added to the summary by RECAP (a correction may quote a value the model wrote).">
              {note}
            </RecapNote>
          ))}
        </div>
      ) : null}

      {agent.error ? (
        <div className="tl-error">
          <Icon name="alert" size={14} />
          <span>
            <span className="muted small">error as recorded (may quote a provider): </span>
            <Mono>{agent.error}</Mono>
          </span>
        </div>
      ) : null}
    </div>
  );
}

// =================================================================================================
// A round: its routing decision, the agents side by side, and the verifier's judgment after it
// =================================================================================================

/** RECAP's leads: where the verifier said the next round should look (trusted RECAP text). */
function Leads({ leads, title }: { leads: string[]; title: string }) {
  return (
    <div className="tl-leads">
      <span className="tl-leads-title">
        {title}{" "}
        <span className="trust-tag trust-trusted" title="Written by RECAP's verifier from checked values: not a disagreement, and it settles nothing.">
          RECAP's text
        </span>
      </span>
      <ul className="tl-questions">
        {leads.map((lead, i) => (
          <li key={i}>{lead}</li>
        ))}
      </ul>
    </div>
  );
}

/** The verifier's open questions and leads this round was given (trusted RECAP text). */
function RoundQuestions({ round }: { round: RoundView }) {
  if (round.questions === null) {
    return (
      <>
        <div className="muted small">The verifier's open questions: not recorded (runs before M3 do not record them).</div>
        {round.leads.length ? <Leads leads={round.leads} title="RECAP's leads for this round" /> : null}
      </>
    );
  }
  if (!round.questions.length) {
    return round.leads.length ? (
      <Notice tone="info" icon="search" title="Given to this round: RECAP's leads">
        <Leads leads={round.leads} title="Where the verifier said to look next" />
      </Notice>
    ) : null;
  }
  const askedOf = round.agents.filter((a) => sameQuestions(a.questions, round.questions)).map((a) => a.role);
  return (
    <Notice
      tone={round.followUp ? "warn" : "info"}
      icon="search"
      title={round.followUp ? "Follow-up round: the verifier's open questions" : "The verifier's open questions for this round"}
    >
      <ul className="tl-questions">
        {round.questions.map((q, i) => (
          <li key={i}>{q}</li>
        ))}
      </ul>
      <div className="muted small">
        RECAP's own text, built from checked values only{askedOf.length ? ` · asked of ${askedOf.join(", ")}` : ""}
        {round.followUp && round.ruleId ? ` · routed by ${round.ruleId}` : ""}
      </div>
      {round.leads.length ? <Leads leads={round.leads} title="RECAP's leads for this round" /> : null}
    </Notice>
  );
}

function IndependentViews({ round }: { round: RoundView }) {
  if (round.independent === true) {
    return (
      <p className="tl-help">
        <Icon name="users" size={13} /> Independent views: each specialist saw what was known before this round plus its own results, never the
        other's evidence while the round ran; the verifier then read all of it. Evidence IDs say who recorded each item:{" "}
        {round.agents.map((a, i) => (
          <span key={a.role}>
            {i ? ", " : ""}
            <Mono nowrap>…{firstIdSuffix(round.round, a.role)}</Mono> is {shortRole(a.role)}'s first
          </span>
        ))}
        .
      </p>
    );
  }
  if (round.independent === null && round.agents.every((a) => a.status !== "running")) {
    return <div className="muted small">Independent views: not recorded (runs before M3 do not record them).</div>;
  }
  return null;
}

/** The verifier's report right after this round (trusted RECAP text). */
function VerifierAfter({ round, step, merge, ctx }: { round: number; step: VerifierStep; merge: dto.TimelineEntry | null; ctx: ViewContext }) {
  return (
    <div className="tl-after">
      <div className="tl-after-head">
        <h4 className="mini-title">The verifier after round {round}</h4>
        <HypothesisChip status={step.status} />
        <span>{step.summary || "—"}</span>
      </div>
      {merge ? (
        <div className="tl-merge">
          <LineView line={lineOf(toTraceEvent(merge, ctx.runId), ctx.feed)} author={ctx.author} />
        </div>
      ) : null}
      {step.disagreements.length ? (
        <ul className="tl-disagreements">
          {step.disagreements.map((d, i) => (
            <li key={i}>
              {d.resolved ? <Chip tone="ok">settled</Chip> : <Chip tone="warn">open</Chip>}
              {d.resolved
                ? d.ruleIds.map((id) => (
                    <Chip key={id} mono title={ruleNote(id)}>
                      {id}
                    </Chip>
                  ))
                : null}
              <span>
                {humanize(d.kind)}
                {d.claims.length ? `: ${d.claims.join(" vs ")}` : ""}
              </span>
              {d.resolution ? <span className="muted">{d.resolution}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {step.leads.length ? <Leads leads={step.leads} title="RECAP's leads for the next round" /> : null}
    </div>
  );
}

function EscalationToken({ escalation }: { escalation: Escalation }) {
  switch (escalation.kind) {
    case "none":
      return null;
    case "not_recorded":
      return <span className="muted small">escalation not recorded</span>;
    case "inconclusive":
      return (
        <StatusToken
          size="sm"
          tone="warn"
          icon="refresh"
          label="escalated"
          detail={`round ${escalation.round} ended ${escalation.status ?? "without a supported cause"}`}
          title="The round before ended without a supported root cause, so the controller ran another round"
        />
      );
    case "failed_fix":
      return (
        <StatusToken size="sm" tone="bad" icon="refresh" label="escalated" detail="an earlier fix did not recover the system" title="The oracle saw no recovery after the fix before this round" />
      );
    case "recorded":
      return <StatusToken size="sm" tone="warn" icon="refresh" label="escalated" title="The trace records an escalation; the events before it do not show why" />;
  }
}

function RoundBand({
  segment,
  round,
  info,
  lanes,
  escalation,
  ctx,
  scripted,
}: {
  segment: Segment;
  round: RoundView | undefined;
  info: dto.RoundInfo | undefined;
  lanes: dto.AgentLane[];
  escalation: Escalation;
  ctx: ViewContext;
  scripted: boolean;
}) {
  const number = segment.round ?? 0;
  const turns = useMemo(() => agentTurns(segment.entries), [segment.entries]);
  const routing = round?.routing;
  const p = (routing?.payload ?? {}) as RoutingPayload;
  const mode = info?.mode || round?.mode || "?";
  const reason = info?.reason || round?.reason || "";
  const ruleId = info?.rule_id || round?.ruleId || "";
  const order = round?.agents.map((a) => a.role) ?? info?.agents ?? [];
  const geometry = roundGeometry(lanes, order);
  const totals = requestTotals(segment.entries);
  const merge = [...segment.entries].reverse().find((e) => e.category === "verification.merge") ?? null;
  const status = info?.verification_status ?? round?.verifier?.status ?? null;
  const agents: AgentView[] = round?.agents ?? [];
  const id = `tl-round-${segment.key}`;
  return (
    <section className="tl-band" aria-labelledby={id}>
      <header className="tl-band-head">
        <h3 id={id} className="tl-band-title">
          Round {number}
        </h3>
        <StatusToken size="sm" tone={mode === "parallel" ? "info" : "neutral"} icon="users" label={mode} title="How the router ran this round" />
        {ruleId ? (
          <Chip mono title="The routing rule that decided this round">
            {ruleId}
          </Chip>
        ) : (
          <span className="muted small">no routing decision recorded</span>
        )}
        {round?.followUp ? (
          <Chip
            tone="warn"
            title="The last round left claims that conflict (level root causes, or different bad releases) and no evidence rule settled them (the router's disagreement signal, whichever rule fired)"
          >
            follow-up for an open disagreement
          </Chip>
        ) : null}
        {round?.judgedBy.kind === "earlier" ? (
          <Chip title={`Fix ${round.judgedBy.fix} did not recover the system, so RECAP started over after it: later verifications judged only newer findings.`}>
            before fix {round.judgedBy.fix}
          </Chip>
        ) : null}
        <EscalationToken escalation={escalation} />
        <span className="tl-band-verdict">
          {status ? (
            <StatusToken size="sm" tone={hypothesisTone(status)} label={`verifier: ${status}`} title="What the verifier concluded after this round" />
          ) : (
            <span className="muted small">{ctx.final ? "no verification recorded" : "not verified yet"}</span>
          )}
        </span>
      </header>
      {reason ? (
        <p className="tl-band-reason">
          <span className="tl-band-label">Router</span> {reason}
          {p.router ? <span className="muted small"> · {p.router}</span> : null}
        </p>
      ) : null}
      {info?.focus_services.length || round?.focusServices.length ? (
        <div className="muted small">
          focus services: <span className="mono">{(info?.focus_services.length ? info.focus_services : (round?.focusServices ?? [])).join(", ")}</span>
        </div>
      ) : null}
      {round ? <RoundQuestions round={round} /> : null}
      {geometry ? <RoundLanes geometry={geometry} overlapMs={info?.overlap_ms ?? null} round={number} replay={ctx.replay} /> : null}
      {mode === "parallel" && totals.withWait > 0 && totals.waitMs !== null ? (
        <p className="tl-help tl-concurrency">
          <Icon name="info" size={13} /> Overlapping turns are not concurrent inference: this round's {fmtInt(totals.count)} model request
          {totals.count === 1 ? "" : "s"} waited {fmtMs(totals.waitMs)} in total inside the model server
          {ctx.replay ? " in the original run" : ""} (its time minus its compute, as recorded).
        </p>
      ) : null}
      {round ? <IndependentViews round={round} /> : null}
      {agents.length ? (
        <div className={agents.length > 1 ? "tl-agents" : "tl-agents single"}>
          {agents.map((agent) => (
            <AgentColumn
              key={agent.role}
              agent={agent}
              round={round as RoundView}
              lane={lanes.find((l) => l.agent === agent.role) ?? null}
              turn={turns.get(agent.role)}
              roundEntries={segment.entries}
              ctx={ctx}
              scripted={scripted}
            />
          ))}
        </div>
      ) : (
        <span className="muted">No agent activity recorded for this round.</span>
      )}
      {round?.verifier ? <VerifierAfter round={number} step={round.verifier} merge={merge} ctx={ctx} /> : null}
      {routing ? (
        <details className="tl-more">
          <summary>
            <Icon name="chevronRight" size={13} /> How the router chose this round: its signals, the step allocation and how the agents ran
          </summary>
          <RoutingDetails event={routing} orchestration={round?.orchestration} />
        </details>
      ) : null}
    </section>
  );
}

// =================================================================================================
// Before the first round, and what followed a round (the fix, the approval, the write, the oracle)
// =================================================================================================

interface FlowItem {
  icon: IconName;
  tone: Tone;
  title: ReactNode;
  body?: ReactNode;
}

function ProposalBody({ proposal, payload, ctx }: { proposal: MitigationProposal; payload: ProposalPayload; ctx: ViewContext }) {
  const source = str(proposal.source);
  const author = source === "redteam" ? "the red-team planner (a deliberately compromised planner)" : ctx.author.label;
  const declared = str(proposal.risk_level);
  const trusted = str(payload.trusted_risk);
  return (
    <div className="tl-flow-detail">
      <div className="chip-row">
        <span className="muted small">source</span>
        <Chip mono>{source || "—"}</Chip>
        <span className="muted small">declared risk</span>
        <Chip mono title="What the proposal itself says (untrusted)">
          {declared || "—"}
        </Chip>
        <span className="muted small">trusted risk</span>
        <Chip mono tone={riskTone(trusted)} title="The risk RECAP's policy assigns to this action (trusted config), whatever the proposal declared">
          {trusted || "—"}
        </Chip>
        {payload.digest ? (
          <span className="muted small" title="The approval is bound to this digest of the exact action, target and parameters">
            digest <Mono>{shortDigest(payload.digest, 12)}…</Mono>
          </span>
        ) : null}
      </div>
      {proposal.rationale ? (
        <div className="tl-quote-block">
          <span className="tl-note-label">Rationale · written by {author}</span>
          <UntrustedText text={proposal.rationale} author={author} />
        </div>
      ) : null}
      <div className="tl-ev-row">
        <span className="tl-ev-label">cites evidence</span>
        <EvLinks ids={arr<string>(proposal.evidence_ids)} runId={ctx.runId} empty={<span className="muted small">none</span>} />
      </div>
      {arr(proposal.citations).length ? (
        <div className="tl-ev-row">
          <span className="tl-ev-label">runbook citations</span>
          {arr<string>(proposal.citations).map((id) => (
            <Chip key={id} mono tone={ctx.overlayIds.has(id) ? "warn" : "neutral"}>
              {id}
            </Chip>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function flowItem(entry: dto.TimelineEntry, segment: Segment, ctx: ViewContext): FlowItem | null {
  const p = entry.payload ?? {};
  switch (entry.kind) {
    case "lifecycle": {
      const q = p as StatePayload;
      const to = str(q.to, "?");
      const diagnosis = diagnosisReason(str(q.reason));
      return {
        icon: "arrowRight",
        tone: stateTone(to),
        title: (
          <>
            {q.from ? `${stateLabel(str(q.from))} → ` : ""}
            <strong>{stateLabel(to)}</strong>
          </>
        ),
        body: diagnosis ? (
          <span>
            diagnosis: <UntrustedText text={diagnosis.claim} author={ctx.author.label} />
          </span>
        ) : q.reason ? (
          <span className="muted">{q.reason}</span>
        ) : undefined,
      };
    }
    case "retrieval": {
      const q = p as RetrievalPayload;
      const results = arr<DocRef>(q.results);
      return {
        icon: "book",
        tone: "info",
        title: q.stage === "proposal" ? "Looked up runbooks for the fix" : `Runbook search (${str(q.stage, "?")})`,
        body: (
          <span>
            {q.reason ? <span className="muted">{q.reason} · </span> : null}
            {results.length ? (
              <span className="chip-row">
                {results.slice(0, 4).map((r) => (
                  <Chip key={str(r.chunk_id)} mono tone={ctx.overlayIds.has(str(r.chunk_id)) ? "warn" : "neutral"}>
                    {str(r.chunk_id, "?")}
                  </Chip>
                ))}
                {results.length > 4 ? <span className="muted small">+{results.length - 4} more</span> : null}
              </span>
            ) : (
              "no results"
            )}
          </span>
        ),
      };
    }
    case "context": {
      const pack = contextPack(entry);
      return {
        icon: "package",
        tone: "neutral",
        title: pack.stage === "proposal" ? "Context for drafting the fix" : "Context pack",
        body: (
          <span className="chip-row">
            {pack.tokensAfter !== null && pack.budget !== null ? (
              <Meter value={pack.tokensAfter} max={pack.budget} width={80} label={`${fmtInt(pack.tokensAfter)} / ${fmtInt(pack.budget)} tokens (RECAP's estimate)`} />
            ) : (
              <NotRecorded what="tokens not recorded" />
            )}
            <span className="muted small">{pack.items.length} items</span>
          </span>
        ),
      };
    }
    case "model_request":
      return entry.model_request
        ? {
            icon: "zap",
            tone: entry.model_request.ok ? "info" : "bad",
            title: entry.model_request.purpose === "propose" ? "The model drafted the fix" : "Model request",
            body: <RequestLine mr={entry.model_request} ctx={ctx} label={entry.model_request.purpose === "propose" ? "Proposal request" : "Model request"} />,
          }
        : null;
    case "proposal": {
      const q = p as ProposalPayload;
      if (!q.proposal) {
        return { icon: "bulb", tone: "bad", title: "No fix could be drafted", body: arr<string>(q.notes).length ? <span className="muted">{arr<string>(q.notes).join("; ")}</span> : undefined };
      }
      const params = obj(q.proposal.params);
      return {
        icon: "bulb",
        tone: "accent",
        title: (
          <>
            Proposed <Mono>{`${str(q.proposal.action, "?")} on ${str(q.proposal.target, "?")}`}</Mono>
            {Object.keys(params).length ? <Mono>{` (${fmtArgs(params)})`}</Mono> : null}
          </>
        ),
        body: <ProposalBody proposal={q.proposal} payload={q} ctx={ctx} />,
      };
    }
    case "policy": {
      const q = p as PolicyPayload;
      const verdict = str(q.verdict, "?");
      const rules = arr<string>(q.rule_ids);
      const sent = segment.entries.some((e) => e.kind === "write_started" && e.tool === entry.tool && e.seq > entry.seq);
      return {
        icon: "shield",
        tone: verdictTone(verdict),
        title: (
          <>
            Policy: <strong>{verdictLabel(verdict)}</strong> <Mono>{str(q.tool, "?")}</Mono>
            {q.risk ? <span className="muted"> · risk {q.risk}</span> : null}
          </>
        ),
        body: (
          <span className="tl-flow-detail">
            {rules.length ? (
              <span>
                deciding rule <Mono>{rules[rules.length - 1]}</Mono> <span className="muted small">({rules.length} rules checked)</span>
              </span>
            ) : null}
            {arr<string>(q.reasons).length ? <span className="muted">{arr<string>(q.reasons).join("; ")}</span> : null}
            {verdict === "DENY" && !sent ? (
              <span className="text-ok">
                <Icon name="shield" size={12} /> Unsafe action attempted and blocked: no write was started.
              </span>
            ) : null}
          </span>
        ),
      };
    }
    case "verification": {
      const q = p as EvidenceCheckPayload;
      if (!q.proposal_check) return null; // a round's verification is shown with its round
      const ok = q.proposal_check.ok === true;
      const problems = arr<string>(q.proposal_check.problems);
      const rules = arr<string>(q.proposal_check.rule_ids);
      return {
        icon: "search",
        tone: ok ? "ok" : "bad",
        title: (
          <>
            Evidence gate: <strong>{ok ? "passed" : "refused"}</strong>
            {q.enforced === false ? <span className="text-warn"> · not enforced (safety-off ablation)</span> : null}
          </>
        ),
        body: (
          <span className="tl-flow-detail">
            {rules.length ? (
              <span className="chip-row">
                {rules.map((id) => (
                  <Chip key={id} mono tone={ok ? "ok" : "neutral"}>
                    {id}
                  </Chip>
                ))}
              </span>
            ) : null}
            {problems.length ? <span className="text-bad">{problems.join("; ")}</span> : null}
          </span>
        ),
      };
    }
    case "approval_requested": {
      const q = p as ApprovalRequestedPayload;
      return {
        icon: "key",
        tone: "warn",
        title: "Approval requested",
        body: (
          <span className="tl-flow-detail">
            <span>
              via <Mono>{str(q.channel, "?")}</Mono>
              {q.expires_at ? ` · expires ${fmtDateTime(q.expires_at)}` : ""}
            </span>
            {q.digest ? (
              <span className="muted small">
                bound to digest <Mono>{shortDigest(q.digest, 12)}…</Mono> of this exact action, target and parameters (single use)
              </span>
            ) : null}
          </span>
        ),
      };
    }
    case "approval_decided": {
      // The note is RECAP's own record of how the answer came ("interactive terminal; typed the
      // target service's name; answered after 167.8 s"): it proves a terminal (or a token holder)
      // and a timing, not who the person was.
      const q = p as ApprovalDecidedPayload & { note?: unknown };
      const decision = str(q.decision, "?");
      const note = str(q.note);
      return {
        icon: "key",
        tone: decision === "approved" ? "ok" : "bad",
        title: (
          <>
            Approval <strong>{decision}</strong>
            {q.actor ? ` by ${q.actor}` : ""}
          </>
        ),
        body:
          q.approval_id || note ? (
            <span className="tl-flow-detail">
              {q.approval_id ? (
                <span className="muted small">
                  approval <Mono>{q.approval_id}</Mono>
                </span>
              ) : null}
              {note ? (
                <span
                  className="small"
                  title="RECAP's record of how the decision came: it shows a terminal (or the API) and a timing, not who the person was"
                >
                  <span className="muted">how: </span>
                  {note}
                </span>
              ) : null}
            </span>
          ) : undefined,
      };
    }
    case "write_started": {
      const q = p as MitigationExecutedPayload;
      const params = obj(q.params);
      const live = ctx.target.state === "live";
      return {
        icon: "zap",
        tone: "accent",
        title: (
          <>
            Sending <Mono>{`${str(q.tool, "?")} on ${str(q.target, "?")}`}</Mono>
            {Object.keys(params).length ? <Mono>{` (${fmtArgs(params)})`}</Mono> : null}
          </>
        ),
        body:
          q.approval_id || live ? (
            <span className="tl-flow-detail">
              {q.approval_id ? (
                <span className="muted small">
                  the gateway's write, with approval <Mono>{q.approval_id}</Mono>
                </span>
              ) : null}
              {live ? (
                <span className="small">
                  to the live target <strong>{liveTargetWords(ctx.target).place}</strong> ({liveTargetWords(ctx.target).real}, not a simulation)
                </span>
              ) : null}
            </span>
          ) : undefined,
      };
    }
    case "write_executed": {
      const q = p as MitigationExecutedPayload;
      const effects = arr<string>(q.side_effects).filter((effect) => typeof effect === "string" && effect);
      const params = obj(q.params);
      const live = ctx.target.state === "live";
      return {
        icon: "zap",
        tone: q.ok === false ? "bad" : "ok",
        title: (
          <>
            {q.ok === false ? "The write failed: " : "Executed "}
            <Mono>{`${str(q.tool, "?")} on ${str(q.target, "?")}`}</Mono>
            {Object.keys(params).length ? <Mono>{` (${fmtArgs(params)})`}</Mono> : null}
            <span className="muted"> · {fmtMs(entry.duration_ms)}</span>
          </>
        ),
        body: (
          <span className="tl-flow-detail">
            {effects.length && live ? (
              // A Kubernetes write is confirmed by reading the Deployment back: its new generation
              // and the image its pods now run (recap/environments/kubernetes/engine.py).
              <span className="tl-effects">
                <span className="tl-note-label">What changed on {liveTargetWords(ctx.target).place}, as RECAP read it back</span>
                <ul className="tl-effect-list">
                  {effects.map((effect, i) => (
                    <li key={i}>
                      <code className="mono">{effect}</code>
                    </li>
                  ))}
                </ul>
              </span>
            ) : effects.length ? (
              <span>{effects.join("; ")}</span>
            ) : null}
            {q.error ? <span className="text-bad">{q.error}</span> : null}
            {entry.evidence_ids.length ? (
              <span>
                recorded as <EvLinks ids={entry.evidence_ids} runId={ctx.runId} />
              </span>
            ) : null}
            {q.run_cancelled ? <span className="text-warn">The run was cancelled while this write was in flight.</span> : null}
          </span>
        ),
      };
    }
    case "oracle": {
      const q = p as RecoveryPayload;
      const still = arr<string>(q.unhealthy_after);
      // How often and how long it probed (M5): a live target gets a settle window, the simulator one probe.
      const settle = settleWords(p, ctx.target);
      return {
        icon: "heart",
        tone: q.verified ? "ok" : "bad",
        title: (
          <>
            Recovery oracle: <strong>{q.verified ? "recovered" : "not recovered"}</strong> <Mono>{str(q.oracle, "oracle")}</Mono>
          </>
        ),
        body: (
          <span className="muted">
            healthy before {yesNo(q.healthy_before)} → after {yesNo(q.healthy_after)}
            {still.length ? ` · still unhealthy: ${still.join(", ")}` : ""}
            {settle ? ` · ${settle}` : q.simulated === true ? " · simulated environment" : q.simulated === false ? " · live target" : ""}
          </span>
        ),
      };
    }
    case "memory": {
      if (entry.category === "memory.consolidated") {
        const q = p as MemoryWritePayload;
        return {
          icon: "save",
          tone: q.verified ? "ok" : "neutral",
          title: (
            <>
              Episode written <Mono>{str(q.episode_id, "?")}</Mono>
            </>
          ),
          body: (
            <span className="muted">
              outcome {str(q.outcome, "?")} · {q.verified ? "oracle-verified" : "not verified"}
              {q.partition ? ` · partition ${q.partition}` : ""}
            </span>
          ),
        };
      }
      break;
    }
    case "budget": {
      const q = p as BudgetPayload;
      return {
        icon: "stop",
        tone: "warn",
        title: (
          <>
            Budget stop: <Mono>{str(q.limit, "?")}</Mono>
          </>
        ),
        body: q.detail ? <span className="muted">{q.detail}</span> : undefined,
      };
    }
    case "error":
      return {
        icon: "x",
        tone: "bad",
        title: "Run error",
        body: <UntrustedText text={str((p as { error?: unknown }).error, "no error text recorded")} author="the run's error (it may quote a provider or a tool)" />,
      };
    case "summary": {
      const q = p as RunSummaryPayload;
      const uses = budgetUses(q.budget);
      return {
        icon: "flag",
        tone: stateTone(str(q.state)),
        title: (
          <>
            Run finished: <strong>{stateLabel(str(q.state, "?"))}</strong>
            {q.outcome_note ? <span className="muted"> · {q.outcome_note}</span> : null}
          </>
        ),
        body: (
          <span className="tl-flow-detail">
            <span className="chip-row">
              <span className="muted small">unsafe actions</span>
              <Chip>attempted {fmtInt(num(q.unsafe_attempts))}</Chip>
              <Chip tone={num(q.unsafe_blocked) ? "ok" : "neutral"}>blocked {fmtInt(num(q.unsafe_blocked))}</Chip>
              <Chip tone={num(q.unsafe_executed) ? "bad" : "neutral"}>executed {fmtInt(num(q.unsafe_executed))}</Chip>
            </span>
            {uses.length ? (
              <span className="tl-budget">
                {uses.map((use) => (
                  <Meter key={use.key} value={use.used} max={use.limit} width={64} label={use.text} />
                ))}
              </span>
            ) : null}
            {q.cost ? <span className="muted">{q.cost}</span> : null}
          </span>
        ),
      };
    }
    default:
      break;
  }
  // Everything else: one described line.
  const line = lineOf(toTraceEvent(entry, ctx.runId), ctx.feed);
  return { icon: line.icon, tone: line.tone, title: <LineView line={line} author={ctx.author} /> };
}

/** Routine events kept in the event log but left out of the story (they add nothing a reader needs). */
function routine(entry: dto.TimelineEntry): boolean {
  return (entry.kind === "policy" && entry.verdict === "ALLOW" && entry.agent !== "controller") || entry.kind === "agent_decision";
}

function FlowList({ segment, ctx }: { segment: Segment; ctx: ViewContext }) {
  const items = segment.entries
    .filter((entry) => !routine(entry))
    .map((entry) => ({ entry, item: flowItem(entry, segment, ctx) }))
    .filter((x): x is { entry: dto.TimelineEntry; item: FlowItem } => x.item !== null);
  return (
    <ol className="tl-flow">
      {items.map(({ entry, item }) => (
        <li key={entry.seq} className={`tl-flow-item tone-${item.tone}`}>
          <span className="tl-flow-icon" aria-hidden="true">
            <Icon name={item.icon} size={14} />
          </span>
          <div className="tl-flow-body">
            <div className="tl-flow-title">
              <span className="tl-flow-time">{fmtOffset(offsetOf(entry, ctx.start))}</span>
              <span>{item.title}</span>
            </div>
            {item.body ? <div className="tl-flow-more">{item.body}</div> : null}
          </div>
        </li>
      ))}
    </ol>
  );
}

function segmentTitle(segment: Segment): { title: string; icon: IconName } {
  const kinds = new Set(segment.entries.map((e) => e.kind));
  if (kinds.has("write_executed") || kinds.has("oracle")) return { title: "The fix: proposal, checks, approval, write and recovery check", icon: "zap" };
  if (kinds.has("proposal")) return { title: "The fix: proposal and checks", icon: "bulb" };
  if (kinds.has("budget")) return { title: "Stopped by a budget limit", icon: "stop" };
  if (kinds.has("error")) return { title: "The run stopped with an error", icon: "x" };
  return { title: `After round ${segment.after ?? "?"}`, icon: "arrowRight" };
}

function SetupSegment({ segment, ctx }: { segment: Segment; ctx: ViewContext }) {
  const created = segment.entries.find((e) => e.kind === "lifecycle" && !obj(e.payload).from);
  const incident = obj(created ? obj(created.payload).incident : null);
  const discovery = segment.entries.find((e) => e.kind === "tool_discovery");
  const memory = segment.entries.find((e) => e.category === "memory.search");
  const hits = memory ? arr((memory.payload as MemorySearchPayload).hits).length : null;
  return (
    <section className="tl-band tl-band-setup" aria-label="Before the first round">
      <details>
        <summary className="tl-band-summary">
          <Icon name="chevronRight" size={13} />
          <span className="tl-band-title">Before the first round</span>
          <span className="muted small">
            {str(incident.service) ? `alert on ${str(incident.service)}` : "the alert"}
            {discovery ? ` · ${arr(obj(discovery.payload).usable).length} MCP tools usable` : ""}
            {hits !== null ? ` · ${hits} similar past episode${hits === 1 ? "" : "s"}` : ""} · {segment.entries.length} events
          </span>
        </summary>
        <FlowList segment={segment} ctx={ctx} />
      </details>
    </section>
  );
}

function AfterSegment({ segment, ctx }: { segment: Segment; ctx: ViewContext }) {
  const { title, icon } = segmentTitle(segment);
  const id = `tl-after-${segment.key}`;
  return (
    <section className="tl-band tl-band-after" aria-labelledby={id}>
      <header className="tl-band-head">
        <Icon name={icon} size={15} />
        <h3 id={id} className="tl-band-title">
          {title}
        </h3>
        <span className="muted small">after round {segment.after ?? "?"}</span>
      </header>
      <FlowList segment={segment} ctx={ctx} />
    </section>
  );
}

/** Who wrote the text below, and which verification the claims' verdicts come from. */
function RoundsIntro({ record, rounds, ctx }: { record: RunRecord | null; rounds: RoundView[]; ctx: ViewContext }) {
  if (!record) {
    return (
      <div className="tl-intro">
        <span className="muted">Claims and their verdicts appear once the run record loads.</span>
      </div>
    );
  }
  const report = record.verification;
  const epochs = rounds.some((r) => r.judgedBy.kind === "earlier");
  return (
    <div className="tl-intro">
      <div>
        Claims, confidence notes, summaries and search queries were written by <strong>{ctx.author.label}</strong>: untrusted data, shown as text and
        never followed (a note RECAP added to a summary is shown apart, as RECAP's). Questions, leads and verdicts are RECAP's own text. Rule lines are
        RECAP's sentences too, but one can repeat a value a claim named (its release version), which the agent wrote: such a line is tagged.
      </div>
      <div className="chip-row">
        <span className="muted">
          Each claim's verdict comes from the last verification that read it
          {epochs ? " (for a claim from before a fix that did not recover the system: the verification before that fix)" : ""}. The{" "}
          {ctx.final ? "final verification" : "latest verification (the run is still going)"}:
        </span>
        {report ? (
          <>
            <HypothesisChip status={report.status} />
            <span>{report.summary || "—"}</span>
          </>
        ) : (
          <span className="muted">none recorded yet</span>
        )}
      </div>
    </div>
  );
}

// =================================================================================================
// The event log: every stored event, filtered and paged by the server
// =================================================================================================

function LogDetail({ entry, ctx }: { entry: dto.TimelineEntry; ctx: ViewContext }) {
  const rows: [ReactNode, ReactNode][] = [
    ["Category", <Mono key="c">{entry.category}</Mono>],
    ["Stage", <Mono key="s">{entry.stage}</Mono>],
    ["Agent", entry.agent ? <Mono key="a">{entry.agent}</Mono> : <span key="a" className="muted">none (RECAP's controller)</span>],
    ["Round", entry.round ?? <span key="r" className="muted">outside a round</span>],
  ];
  if (entry.step !== null) rows.push(["Step", entry.step]);
  if (entry.tool) rows.push(["Tool", <Mono key="t">{entry.tool}</Mono>]);
  if (entry.invocation_id) rows.push(["Invocation", <Mono key="i">{entry.invocation_id}</Mono>]);
  if (entry.verdict) rows.push(["Verdict", <Mono key="v">{entry.verdict}</Mono>]);
  if (entry.ok !== null) rows.push(["OK", yesNo(entry.ok)]);
  if (entry.duration_ms !== null) rows.push(["Duration", fmtMs(entry.duration_ms)]);
  if (entry.evidence_ids.length) rows.push(["Evidence", <EvLinks key="e" ids={entry.evidence_ids} runId={ctx.runId} />]);
  rows.push(["Recorded at", <Mono key="ts">{entry.ts}</Mono>]);
  return (
    <div className="tl-log-detail">
      <KeyValues rows={rows} className="kv-wide" />
      {entry.model_request ? <RequestLine mr={entry.model_request} ctx={ctx} /> : null}
      <div className="tl-raw">
        <div className="tl-raw-head">
          <span className="log-label">The event as stored</span>
          {entry.untrusted_text ? (
            <span className="untrusted-tag" title="This event holds text a model, a tool or an alert wrote: shown as data, never followed">
              untrusted text inside
            </span>
          ) : null}
        </div>
        <JsonView value={entry.payload} maxHeight={260} />
      </div>
    </div>
  );
}

function LogRow({ entry, ctx }: { entry: dto.TimelineEntry; ctx: ViewContext }) {
  const [open, setOpen] = useState(false);
  const line = useMemo(() => lineOf(toTraceEvent(entry, ctx.runId), ctx.feed), [entry, ctx.runId, ctx.feed]);
  return (
    <li className={`tl-log-row lane-${laneKey(entry.agent)} tone-${line.tone}`}>
      <button type="button" className="tl-log-line" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="tl-log-seq">#{entry.seq}</span>
        <span className="tl-log-time">{fmtOffset(offsetOf(entry, ctx.start))}</span>
        <LaneTag agent={entry.agent} />
        <span className="tl-log-kind">
          <Icon name={line.icon} size={12} />
          {kindLabel(entry.kind)}
          {entry.round !== null ? <span className="tl-log-round">R{entry.round}</span> : null}
        </span>
        <span className="tl-log-text">
          <LineView line={line} author={ctx.author} />
        </span>
      </button>
      {open ? <LogDetail entry={entry} ctx={ctx} /> : null}
    </li>
  );
}

function option(value: string, label: string, count: number): FilterOption {
  return { value, label, count };
}

function EventLog({
  log,
  filters,
  entries,
  view,
  ctx,
  onFilter,
  onClearFilters,
}: {
  log: LogLoad;
  filters: LogFilters;
  entries: readonly dto.TimelineEntry[];
  view: dto.TimelineView;
  ctx: ViewContext;
  onFilter: (key: string, value: string | null) => void;
  onClearFilters: () => void;
}) {
  const counts = useMemo(() => filterCounts(entries), [entries]);
  const specs: FilterSpec[] = [
    {
      key: "agent",
      label: "Agent",
      options: [...counts.agents].sort((a, b) => laneRank(a[0]) - laneRank(b[0])).map(([agent, n]) => option(agent, laneName(agent), n)),
    },
    {
      key: "round",
      label: "Round",
      options: [...counts.rounds].sort((a, b) => a[0] - b[0]).map(([round, n]) => option(String(round), `Round ${round}`, n)),
    },
    {
      key: "tool",
      label: "Tool",
      options: [...counts.tools].sort((a, b) => a[0].localeCompare(b[0])).map(([tool, n]) => option(tool, tool, n)),
      display: "select",
      allLabel: "Any tool",
    },
    {
      key: "kind",
      label: "Kind of event",
      options: Object.entries(view.counts ?? {})
        .sort((a, b) => kindLabel(a[0]).localeCompare(kindLabel(b[0])))
        .map(([kind, n]) => option(kind, kindLabel(kind), n)),
      display: "select",
      allLabel: "Every kind",
    },
  ];
  const values = { agent: filters.agent, round: filters.round !== null ? String(filters.round) : null, tool: filters.tool, kind: filters.kind };
  const state = log.state;
  const active = activeLogFilters(filters);
  const shown = state.entries.length;
  const summary = state.loaded
    ? `Showing ${fmtInt(shown)} of ${fmtInt(state.matching)} ${active ? "matching " : ""}events${active && state.total !== null ? ` (${fmtInt(state.total)} in the trace)` : ""}`
    : "Loading events…";
  const remaining = state.matching !== null ? Math.max(0, state.matching - shown) : null;
  return (
    <>
      <FilterBar filters={specs} values={values} onChange={onFilter} onClear={onClearFilters} summary={summary} label="Event log filters" />
      {log.error && !state.loaded ? <ErrorView error={log.error} title="The event log did not load" onRetry={log.loadMore} /> : null}
      {!state.loaded && !log.error ? <LoadingView label="Loading events…" /> : null}
      {state.loaded && !shown ? (
        <EmptyView
          title={active ? "No events match these filters" : "No events stored for this run"}
          reason={
            active
              ? "The server found no stored event of this run that passes every filter. Clear one to see more."
              : "The server has no stored trace events for this run."
          }
          icon="search"
          action={
            active ? (
              <button type="button" className="btn btn-small" onClick={onClearFilters}>
                Clear filters
              </button>
            ) : undefined
          }
        />
      ) : null}
      {shown ? (
        <ol className="tl-log" aria-label="Trace events">
          {state.entries.map((entry) => (
            <LogRow key={entry.seq} entry={entry} ctx={ctx} />
          ))}
        </ol>
      ) : null}
      {state.loaded && (state.hasMore || state.live) ? (
        <div className="tl-log-more">
          {log.error ? <ErrorView error={log.error} title="The next page did not load" onRetry={log.loadMore} /> : null}
          <button type="button" className="btn btn-small" onClick={log.loadMore} disabled={log.busy}>
            <Icon name={state.hasMore ? "chevronDown" : "refresh"} size={13} />
            {state.hasMore ? `Load ${fmtInt(Math.min(LOG_PAGE_SIZE, remaining ?? LOG_PAGE_SIZE))} more` : "Check for newer events"}
          </button>
          <span className="muted small" role="status">
            {log.busy ? "Loading…" : state.hasMore ? `${fmtInt(remaining)} more after event #${state.cursor}` : "The run is still going: newer events may arrive."}
          </span>
        </div>
      ) : null}
    </>
  );
}

// =================================================================================================
// The page
// =================================================================================================

function jumpTo(id: string): void {
  const element = typeof document === "undefined" ? null : document.getElementById(id);
  if (!element) return;
  element.scrollIntoView({ behavior: "smooth", block: "start" });
  element.focus({ preventScroll: true });
}

function SectionNav() {
  const items: [string, string][] = [
    ["tl-sec-run", "This run"],
    ["tl-sec-timeline", "Run timeline"],
    ["tl-sec-story", "Step by step"],
    ["tl-sec-log", "Event log"],
  ];
  return (
    <nav className="tl-nav" aria-label="On this page">
      {items.map(([id, label]) => (
        <button key={id} type="button" className="tl-nav-btn" onClick={() => jumpTo(id)}>
          {label}
        </button>
      ))}
    </nav>
  );
}

function Anchor({ id, children }: { id: string; children: ReactNode }) {
  return (
    <div id={id} className="tl-anchor" tabIndex={-1}>
      {children}
    </div>
  );
}

export interface TraceExplorerViewProps {
  runId: string;
  trace: TraceLoad;
  detail: Pick<Polled<dto.RunDetail>, "data" | "error" | "loading" | "refresh">;
  log: LogLoad;
  filters: LogFilters;
  onFilter: (key: string, value: string | null) => void;
  onClearFilters: () => void;
}

/** The Trace Explorer for one run, from what has loaded (no fetching here: the tests render it directly). */
export function TraceExplorerView({ runId, trace, detail, log, filters, onFilter, onClearFilters }: TraceExplorerViewProps) {
  const view = trace.state.view;
  const entries = trace.state.entries;
  const record = detail.data?.record ?? null;
  const summary = detail.data?.summary ?? null;
  const evidence = detail.data ? detail.data.evidence : null;
  const running = !!view?.live;

  const events = useMemo(() => entries.map((e) => toTraceEvent(e, runId)), [entries, runId]);
  const model = useMemo(() => buildTimeline(events), [events]);
  const rounds = useMemo(() => buildRounds({ events, record, evidence, running }), [events, record, evidence, running]);
  const segments = useMemo(() => segmentsOf(entries), [entries]);
  const overlays = knowledgeOverlays(record?.versions);
  const folders = overlays.join("\n");
  const feed = useMemo<FeedContext>(
    () => ({ notes: modelNotesByAgent(events), isOverlay: (path) => isOverlayPath(path, folders ? folders.split("\n") : []) }),
    [events, folders],
  );

  if (!view) {
    if (trace.error) return <ErrorView error={trace.error} title="The trace did not load" onRetry={trace.retry} />;
    return <LoadingView label="Loading the trace…" />;
  }

  const provider: Partial<ProviderInfo> | null =
    record?.provider_info ?? (summary ? { origin: summary.provider_class ?? undefined, kind: summary.provider_kind ?? undefined } : null);
  const replay = summary?.provider_class === "replay" || provider?.origin === "replay" || allReplayed(entries);
  const scripted = provider?.origin === "scripted";
  const pace = num(obj(record?.config).replay_pace);
  const ctx: ViewContext = {
    runId,
    author: textAuthor(provider),
    final: !!record && isTerminal(record.state),
    multiAgent: rounds.some((r) => r.agents.length > 1),
    overlayIds: overlayChunkIds(searchesOf(events), record?.retrieved_docs ?? [], overlays),
    replay,
    start: traceStart(entries),
    feed,
    target: targetFacts(runTargetOf(summary, record)),
  };
  const roundSegments = segments.filter((s) => s.kind === "round");

  return (
    <div className="tl-page">
      <SectionNav />
      {trace.error ? (
        <Notice tone="warn" icon="alert" title="Showing the events loaded so far">
          {trace.error.message}{" "}
          <button type="button" className="btn btn-small" onClick={trace.retry}>
            <Icon name="refresh" size={13} /> Retry
          </button>
        </Notice>
      ) : null}
      {trace.state.truncated ? (
        <Notice tone="warn" icon="alert" title={`This run has ${fmtInt(view.total_events)} events: the views below use the first ${fmtInt(entries.length)}`}>
          The event log still pages through every event, and the counts above are the server's, over the whole trace.
        </Notice>
      ) : null}

      <Anchor id="tl-sec-run">
        <RunFacts summary={summary} record={record} view={view} entries={entries} replay={replay} pace={pace && pace > 0 ? pace : null} />
      </Anchor>
      {detail.error && !detail.data ? (
        <Notice tone="warn" icon="alert" title="The run record did not load">
          {detail.error.message} Claims, verdicts and who recorded each piece of evidence need it; what follows comes from the trace only.
        </Notice>
      ) : null}

      {!view.trace_available ? (
        <Card title="Trace">
          <EmptyView
            title="No trace events are stored for this run"
            reason="The server has the run's record but no stored trace (a missing or deleted trace file), so there is nothing to draw. Evidence & Decisions still shows what the record kept."
            icon="timeline"
            action={
              <a className="btn btn-small" href={hrefFor("evidence", { run: runId })}>
                Open Evidence & Decisions
              </a>
            }
          />
        </Card>
      ) : (
        <>
          <Anchor id="tl-sec-timeline">
            <Card
              title="Run timeline"
              subtitle="One lane per agent plus the controller. Bars are tool calls (MCP reads, and runbook searches RECAP's own index answers), diamonds are model answers, dots are controller decisions, shaded bands are rounds. Hover or focus a mark for its details."
            >
              {replay ? (
                <p className="tl-help">
                  <Icon name="rewind" size={13} /> The axis is this replay's own time. A replay sends no model request, so its turns take milliseconds; each
                  model answer's recorded duration (the original run's) is in its tooltip and in the steps below.
                </p>
              ) : null}
              {model ? <AgentGantt model={model} author={ctx.author} replay={replay} /> : <EmptyView title="Nothing to draw yet" reason="No event with a valid time is stored yet." icon="timeline" />}
            </Card>
          </Anchor>

          <Anchor id="tl-sec-story">
            <Card
              title="Step by step"
              subtitle="The run in order: what RECAP looked up before the first round, each round's routing decision, the agents' steps side by side, the verifier's judgment after each round, and the fix that followed."
              actions={record ? <OriginChip provider={record.provider_info} /> : undefined}
            >
              {overlays.length ? (
                <Notice tone="warn" icon="alert" title={`This run indexed extra knowledge folders: ${overlays.join(", ")} (red-team)`}>
                  Runbook sections from them may be deliberately wrong; they are marked <OverlayChip /> in the agents' citations and searches below. A runbook
                  never proves a claim: the verifier reads only live tool results.
                </Notice>
              ) : null}
              {roundSegments.length ? <RoundsIntro record={record} rounds={rounds} ctx={ctx} /> : null}
              {segments.length ? (
                segments.map((segment, index) => {
                  if (segment.kind === "setup") return <SetupSegment key={segment.key} segment={segment} ctx={ctx} />;
                  if (segment.kind === "after") return <AfterSegment key={segment.key} segment={segment} ctx={ctx} />;
                  const info = view.rounds.find((r) => r.round === segment.round);
                  return (
                    <RoundBand
                      key={segment.key}
                      segment={segment}
                      round={rounds.find((r) => r.round === segment.round)}
                      info={info}
                      lanes={lanesOf(view.lanes, segment.round ?? 0)}
                      escalation={escalationOf(info, segments, index, view.rounds)}
                      ctx={ctx}
                      scripted={scripted}
                    />
                  );
                })
              ) : (
                <EmptyView title="No events yet" reason="The run has not recorded anything yet." icon="timeline" />
              )}
              {segments.length && !roundSegments.length ? (
                <p className="tl-help">
                  <Icon name="info" size={13} /> No investigation round was recorded: the run stopped before routing its first round.
                </p>
              ) : null}
            </Card>
          </Anchor>
        </>
      )}

      <Anchor id="tl-sec-log">
        <Card
          title="Event log"
          subtitle="Every event RECAP stored for this run, oldest first, 100 at a time. The server applies the filters; click an event for its stored fields."
        >
          <EventLog log={log} filters={filters} entries={entries} view={view} ctx={ctx} onFilter={onFilter} onClearFilters={onClearFilters} />
        </Card>
      </Anchor>
    </div>
  );
}

function TraceExplorer({ runId, route }: { runId: string; route: Route }) {
  const trace = useFullTrace(runId);
  const live = !!trace.state.view?.live;
  const detail = usePolling(() => api.runView(runId), (d) => (d?.status === "running" || live ? 3000 : null), [runId]);
  const params = route.params.toString();
  const filters = useMemo(() => readLogFilters((key) => new URLSearchParams(params).get(key)), [params]);
  const log = useEventLog(runId, filters);

  // A run that is still going adds rounds while it runs: refresh the record when it finishes.
  const { refresh } = detail;
  const wasLive = useRef(live);
  useEffect(() => {
    if (wasLive.current && !live) refresh();
    wasLive.current = live;
  }, [live, refresh]);

  const routeRef = useRef(route);
  routeRef.current = route;
  const onFilter = useCallback((key: string, value: string | null) => {
    const current = routeRef.current;
    navigate(current.page, { ...paramsOf(current), [key]: value }, true);
  }, []);
  const onClearFilters = useCallback(() => {
    const current = routeRef.current;
    navigate(current.page, { ...paramsOf(current), agent: null, round: null, kind: null, tool: null }, true);
  }, []);

  return <TraceExplorerView runId={runId} trace={trace} detail={detail} log={log} filters={filters} onFilter={onFilter} onClearFilters={onClearFilters} />;
}

export function AgentTimelinePage({ route }: PageProps) {
  const selection = useRunSelection(route);
  return (
    <>
      <PageHeader
        title="Trace Explorer"
        description="Every step of one run as RECAP recorded it: how each round was routed, what each agent asked its model and which tools it called, how the verifier judged the claims, and the approval, write and recovery check that followed."
      />
      <RunScope selection={selection} page="timeline">
        {(runId) => <TraceExplorer key={runId} runId={runId} route={route} />}
      </RunScope>
    </>
  );
}
