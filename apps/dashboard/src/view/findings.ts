/**
 * What each agent found, round by round (M3), for the Agent Timeline page.
 *
 * Joins the trace (routing.decision, investigation.round, agent.start/decision/finish, tool
 * calls, runbook searches, verification.evidence, verification.recovery) with the run record
 * (findings, the final verification) and the evidence registry (which agent recorded each item).
 *
 * A claim's verdict comes from the last verification that read it. After a fix that did not
 * recover the system RECAP starts a new epoch, and later verifications judge only the new
 * epoch's findings; a budget stop can keep findings no verification ever read (partial).
 *
 * Text a model, the scripted policy or a tool wrote (claims, confidence notes, summaries,
 * search queries) stays a plain string here: the page shows it as untrusted data. Nothing is
 * inferred that the backend did not record: a field missing on an older run becomes null
 * ("not recorded"), never a guess.
 *
 * This module imports types only, so the Node tests in ../../tests can load it directly.
 */

import type { Tone } from "../format";
import type {
  AgentFinding,
  AgentFinishPayload,
  AgentStartPayload,
  CheckSummary,
  ContextPackPayload,
  Disagreement,
  DocRef,
  Evidence,
  EvidenceCheckPayload,
  Hypothesis,
  HypothesisCheck,
  InvestigationRoundPayload,
  LlmCallPayload,
  OrchestrationAgent,
  OrchestrationPayload,
  ProviderInfo,
  RecoveryPayload,
  RetrievalPayload,
  RoutingPayload,
  RunRecord,
  ToolCallPayload,
  ToolResultPayload,
  TraceEvent,
} from "../types";

// --- defensive readers (payloads come from several backend modules) --------------------------

function list<T = unknown>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function text(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return "";
  }
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function fields(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function unique(items: Iterable<string>): string[] {
  return [...new Set(items)];
}

// --- who wrote an agent's text ------------------------------------------------------------------

export interface TextAuthor {
  kind: "model" | "replay" | "scripted" | "unknown";
  /** Who wrote an agent's claims, notes and queries, e.g. "the model". */
  label: string;
  /** Possessive form, e.g. "the model's". */
  whose: string;
  /** For compact tags, e.g. "model" or "scripted policy". */
  short: string;
}

/**
 * Who wrote the agents' free text in this run. A scripted run must never read as model
 * reasoning, so this follows ProviderInfo.origin (or, before origins existed, its kind),
 * like the run-origin badge in src/format.ts.
 */
export function textAuthor(provider: Partial<ProviderInfo> | null | undefined): TextAuthor {
  const origin: string | undefined = provider?.origin ?? provider?.kind;
  switch (origin) {
    case "scripted":
      return {
        kind: "scripted",
        label: "the scripted policy (not a model)",
        whose: "the scripted policy's",
        short: "scripted policy",
      };
    case "local_llm":
    case "free_hosted_llm":
    case "paid_llm":
    case "llm":
      return { kind: "model", label: "the model", whose: "the model's", short: "model" };
    case "replay":
      return {
        kind: "replay",
        label: "the model, replayed from a recording",
        whose: "the recorded model's",
        short: "recorded model",
      };
    default:
      return { kind: "unknown", label: "the agent", whose: "the agent's", short: "agent" };
  }
}

// --- roles, outcomes, dropped claims -------------------------------------------------------------

/** "telemetry_investigator" -> "telemetry" (a specialist's short name). */
export function shortRole(role: string | null | undefined): string {
  if (!role) return "controller";
  const suffix = "_investigator";
  return role.endsWith(suffix) ? role.slice(0, -suffix.length) : role.replace(/_/g, " ");
}

export interface OutcomeView {
  /** AgentFinding.outcome; "" when not recorded; "running" or "missing" when there is no finding. */
  key: string;
  label: string;
  tone: Tone;
  note: string;
}

// The same tones as the live feed's agent.finish line (format.ts FINDING_OUTCOMES; a test keeps
// them equal): an agent without a usable answer is a missing view, like one that crashed.
const OUTCOMES: Record<string, Omit<OutcomeView, "key">> = {
  hypothesis: { label: "made claims", tone: "accent", note: "Ended its turn with root-cause claims for the verifier." },
  need_more_evidence: {
    label: "needs more evidence",
    tone: "info",
    note: "Ended its turn without a claim it may make: it asked for more evidence.",
  },
  stop: { label: "stopped", tone: "warn", note: "Chose to stop without a claim." },
  step_limit: { label: "out of steps", tone: "warn", note: "Used every step it was given before it finished, so it made no claims." },
  no_usable_answer: { label: "no usable answer", tone: "bad", note: "The model gave nothing RECAP could use." },
  error: { label: "failed", tone: "bad", note: "Timed out or crashed: it has no finding of its own." },
};

const RUNNING: OutcomeView = { key: "running", label: "still working", tone: "accent", note: "No finding yet." };
const MISSING: OutcomeView = {
  key: "missing",
  label: "no finding recorded",
  tone: "warn",
  note: "No finish and no finding were recorded for this agent in this round.",
};

/** How an agent ended its turn (AgentFinding.outcome), in plain words. */
export function outcomeView(outcome: string | null | undefined): OutcomeView {
  const key = outcome ?? "";
  if (!key) {
    return { key: "", label: "outcome not recorded", tone: "neutral", note: "Runs recorded before M3 do not say how an agent ended." };
  }
  const known = OUTCOMES[key];
  return known ? { key, ...known } : { key, label: key.replace(/_/g, " "), tone: "neutral", note: "" };
}

export interface DroppedClaims {
  mechanism: string;
  count: number;
}

/** agent.finish `dropped_out_of_role`, grouped by mechanism; null when the trace does not record it. */
export function droppedClaims(payload: AgentFinishPayload | null | undefined): DroppedClaims[] | null {
  if (!payload || !Array.isArray(payload.dropped_out_of_role)) return null;
  const counts = new Map<string, number>();
  for (const mechanism of payload.dropped_out_of_role) {
    const key = text(mechanism) || "unknown";
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts].map(([mechanism, n]) => ({ mechanism, count: n }));
}

/** RECAP's own words for claims the harness dropped: "telemetry may not conclude bad_deployment". */
export function droppedLine(role: string, dropped: DroppedClaims): string {
  const times = dropped.count > 1 ? ` (${dropped.count} claims)` : "";
  return `${shortRole(role)} may not conclude ${dropped.mechanism}${times}`;
}

// --- evidence --------------------------------------------------------------------------------

export interface EvidenceRef {
  id: string;
  /** The agent that recorded it (Evidence.agent_role, else the tool.result event's agent). */
  agent: string | null;
  /** e.g. "mcp:get_service_logs(service=checkout)"; free text in it is hashed by the backend. */
  sourceRef: string | null;
  sourceType: string | null;
  flags: string[];
  /** False when the registry is loaded and has no such item; null when it has not loaded. */
  known: boolean | null;
}

const PER_AGENT_ID = /-r\d+[a-z]\d{2,}$/;

/** True for an ID from a parallel round's per-agent namespace (ev-3f9a2c-r2t01, ADR 0020). */
export function isPerAgentId(id: string): boolean {
  return PER_AGENT_ID.test(id);
}

// The letter each role's IDs carry inside a parallel round (recap/context/evidence.py _ROLE_CODES).
const ROLE_CODES: Record<string, string> = {
  telemetry_investigator: "t",
  infrastructure_investigator: "i",
  investigator: "v",
};

/** The ID suffix of a role's first evidence item in a parallel round: "-r2t01". */
export function firstIdSuffix(round: number, role: string): string {
  return `-r${round}${ROLE_CODES[role] ?? "x"}01`;
}

// --- runbook searches ------------------------------------------------------------------------------

/** Answered by RECAP's own runbook index, not the environment (`source: local` in configs/policies.yaml). */
export const RUNBOOK_SEARCH_TOOL = "search_runbooks";

export function isRunbookSearch(tool: string | null | undefined): boolean {
  return tool === RUNBOOK_SEARCH_TOOL;
}

export interface SignalView {
  key: string;
  label: string;
  value: string;
}

const SIGNAL_LABELS: Record<string, string> = {
  bm25_rank: "BM25 rank",
  bm25: "BM25",
  dense_rank: "dense rank",
  cosine: "cosine",
  rrf: "RRF",
};

/** A rank as an integer: ranks inside ranking signals arrive as floats (12.0). */
export function rankLabel(value: unknown): string {
  const n = count(value);
  return n === null ? "—" : String(Math.round(n));
}

function scoreLabel(value: number): string {
  return value !== 0 && Math.abs(value) < 0.1 ? value.toFixed(4) : value.toFixed(2);
}

/** A result's ranking signals in the backend's order; ranks as integers, scores rounded. */
export function signalViews(signals: unknown): SignalView[] {
  return Object.entries(fields(signals))
    .filter(([, value]) => count(value) !== null)
    .map(([key, value]) => ({
      key,
      label: SIGNAL_LABELS[key] ?? key.replace(/_/g, " "),
      value: key === "rank" || key.endsWith("_rank") ? rankLabel(value) : scoreLabel(value as number),
    }));
}

export interface SearchHit {
  rank: string;
  chunkId: string;
  /** The runbook file ("" when not recorded): tells overlay (red-team) sections apart. */
  docPath: string;
  /** A runbook heading: document text, so untrusted. */
  heading: string;
  signals: SignalView[];
}

export interface RunbookSearch {
  id: string;
  /** Written by the agent: untrusted. */
  query: string;
  k: number | null;
  status: "ok" | "failed" | "running" | "no_result";
  error: string | null;
  /** The search receipt, recorded as DOC evidence (never counted as proof). */
  evidenceId: string | null;
  /** null when no retrieval.docs event was recorded for the call. */
  hits: SearchHit[] | null;
}

// --- claims and the final verification ----------------------------------------------------------

export interface CheckView {
  status: string;
  /** Every agent that made the claim (M3); null when not recorded. */
  proposers: string[] | null;
  /** The evidence the passing rules relied on (M3); null when not recorded. */
  basis: EvidenceRef[] | null;
  /** The verifier's failed rule lines: RECAP's sentences, which may repeat a value the claim named. */
  failed: string[];
  /** Per failed line: it repeats a value the claim named that is not shaped like a version or a service (model text). */
  failedQuotesClaim: boolean[];
  ruleIds: string[];
  /** What the merger keyed the claim by (HypothesisCheck.detail): its release or blamed dependency; "" when none or not recorded. */
  detail: string;
  /** False when that detail is not shaped like a version or a service name: the claim's own text, shown as data. */
  detailChecked: boolean;
}

/**
 * not_recorded: the run recorded no verification · partial: kept from a round a budget stop cut
 * short · stopped: the run stopped before a verifier read this round · pending: not read yet
 * (the run is still going).
 */
export type UnverifiedReason = "not_recorded" | "partial" | "stopped" | "pending";

export type FinalVerdict =
  | { kind: "unverified"; reason: UnverifiedReason }
  | { kind: "unchecked" } // the verification has no check for this claim
  | { kind: "matched"; check: CheckView }
  | { kind: "ambiguous"; checks: CheckView[] };

export interface ClaimView {
  /** Written by the agent: untrusted. */
  claim: string;
  mechanism: string;
  target: string | null;
  /** e.g. ["version", "v2.4.1"]; values can come from a model or a tool. */
  details: [string, string][];
  origin: string;
  supporting: EvidenceRef[];
  contradicting: EvidenceRef[];
  /** The model's own words on how sure it is (untrusted, never a score); "" when none. */
  confidenceNote: string;
  verdict: FinalVerdict;
}

// Besides origin, mechanism and target, the merger keys a claim by this detail (recap/agents/merger.py).
const DETAIL_KEYS: Record<string, string> = { bad_deployment: "version", dependency_failure: "dependency" };

function detailOf(h: Partial<Hypothesis> | null | undefined): string {
  const key = DETAIL_KEYS[text(h?.mechanism)];
  return key ? text(fields(h?.details)[key]) : "";
}

/** The detail the merger keyed a check by (HypothesisCheck.detail, M3), else the one its claim names. */
function judgedDetail(c: HypothesisCheck): string {
  return text(c.detail) || detailOf(c.hypothesis);
}

/** A version without its case and one leading "v": the verifier judges "2.4.1" as the observed "v2.4.1". */
function looseVersion(version: string): string {
  return version.toLowerCase().replace(/^v/, "");
}

// Values of these shapes are not free text (a version as the verifier reads it, a service name).
const VERSION_SHAPE = /^[vV]?[0-9]+\.[0-9]+\.[0-9]+$/;
const SERVICE_SHAPE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

function checkedValue(mechanism: string, value: string): boolean {
  return mechanism === "dependency_failure" ? SERVICE_SHAPE.test(value) : VERSION_SHAPE.test(value);
}

/** True when `line` repeats `value` as a whole (not inside a longer word). */
function repeats(line: string, value: string): boolean {
  for (let at = line.indexOf(value); at >= 0; at = line.indexOf(value, at + 1)) {
    if (!/\w/.test(line.charAt(at - 1)) && !/\w/.test(line.charAt(at + value.length))) return true;
  }
  return false;
}

/**
 * The verification's checks for one claim: same mechanism and target. The merger also keeps
 * a remembered claim apart from a live one and two release versions (or blamed dependencies)
 * apart, so those must match too when the claim names one: the check's detail equal to it, or,
 * for a release, equal without one leading "v" and its case. More than one result means the
 * record cannot tell which check judged this claim.
 */
export function finalChecksFor(h: Hypothesis, checks: readonly HypothesisCheck[]): HypothesisCheck[] {
  const target = h.target_service ?? "";
  const origin = h.origin || "investigation";
  const base = checks.filter((c) => {
    const ch = c?.hypothesis;
    return (
      !!ch &&
      ch.mechanism === h.mechanism &&
      (ch.target_service ?? "") === target &&
      (ch.origin || "investigation") === origin
    );
  });
  const detail = detailOf(h);
  if (!detail) return base;
  const exact = base.filter((c) => judgedDetail(c) === detail);
  if (exact.length) return exact;
  if (h.mechanism === "bad_deployment") {
    const loose = base.filter((c) => judgedDetail(c) !== "" && looseVersion(judgedDetail(c)) === looseVersion(detail));
    if (loose.length) return loose;
  }
  return base.filter((c) => !judgedDetail(c));
}

type Resolve = (id: string) => EvidenceRef;

/**
 * `claimed`: values the claim named that are not shaped like a version or a service. A rule line
 * that repeats one quotes model text, so the page tags it.
 */
function checkView(c: HypothesisCheck, resolve: Resolve, claimed: readonly string[] = []): CheckView {
  // An M3 check always has a proposer: an empty list is the API's default for a run before M3.
  const recorded = Array.isArray(c.proposers) && c.proposers.length > 0;
  const failed = list(c.failed).map(text);
  const detail = text(c.detail);
  const mechanism = text(c.hypothesis?.mechanism);
  const quoted = unique([...claimed, ...unshapedDetails(c.hypothesis), ...(detail && !checkedValue(mechanism, detail) ? [detail] : [])]);
  return {
    status: text(c.hypothesis?.status) || "?",
    proposers: recorded ? list(c.proposers).map(text) : null,
    basis: recorded ? list(c.basis_evidence_ids).map((id) => resolve(text(id))) : null,
    failed,
    failedQuotesClaim: failed.map((line) => quoted.some((value) => repeats(line, value))),
    ruleIds: list(c.rule_ids).map(text),
    detail,
    detailChecked: !detail || checkedValue(mechanism, detail),
  };
}

/** The release or dependency a claim names, when it is not shaped like a version or a service. */
function unshapedDetails(h: Partial<Hypothesis> | null | undefined): string[] {
  const value = detailOf(h).trim();
  return value && !checkedValue(text(h?.mechanism), value) ? [value] : [];
}

/** The verification's verdict on one claim; `checks` is null when no verification was recorded. */
export function finalVerdict(h: Hypothesis, checks: readonly HypothesisCheck[] | null, resolve: Resolve): FinalVerdict {
  if (checks === null) return { kind: "unverified", reason: "not_recorded" };
  const found = finalChecksFor(h, checks);
  const claimed = unshapedDetails(h);
  if (!found.length) return { kind: "unchecked" };
  if (found.length === 1) return { kind: "matched", check: checkView(found[0], resolve, claimed) };
  return { kind: "ambiguous", checks: found.map((c) => checkView(c, resolve, claimed)) };
}

function claimView(h: Hypothesis, verdict: (h: Hypothesis) => FinalVerdict, resolve: Resolve): ClaimView {
  const details = Object.entries(fields(h.details))
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]): [string, string] => [key, text(value)]);
  return {
    claim: text(h.claim),
    mechanism: text(h.mechanism) || "unknown",
    target: h.target_service ?? null,
    details,
    origin: h.origin || "investigation",
    supporting: list(h.supporting_evidence_ids).map((id) => resolve(text(id))),
    contradicting: list(h.contradicting_evidence_ids).map((id) => resolve(text(id))),
    confidenceNote: text(h.confidence_note).trim(),
    verdict: verdict(h),
  };
}

/**
 * A verification.evidence payload's checks as HypothesisChecks. They carry no details, so a
 * claim naming a release matches every check of its mechanism and service (ambiguous when
 * there are several): never a guess.
 */
function payloadChecks(report: EvidenceCheckPayload): HypothesisCheck[] {
  return list<CheckSummary>(report.checks).map((c) => ({
    hypothesis: {
      claim: text(c.claim),
      mechanism: (text(c.mechanism) || "unknown") as Hypothesis["mechanism"],
      target_service: c.target ?? null,
      supporting_evidence_ids: [],
      contradicting_evidence_ids: [],
      status: (text(c.status) || "proposed") as Hypothesis["status"],
      details: {},
      origin: text(c.origin) === "memory" ? "memory" : "investigation",
      proposed_by: null,
    },
    passed: list(c.passed).map(text),
    failed: list(c.failed).map(text),
    rule_ids: list(c.rule_ids).map(text),
    proposers: Array.isArray(c.proposers) ? (c.proposers.map(text) as HypothesisCheck["proposers"]) : undefined,
    basis_evidence_ids: Array.isArray(c.basis_evidence_ids) ? c.basis_evidence_ids.map(text) : undefined,
  }));
}

// --- rounds ----------------------------------------------------------------------------------------

export interface AgentView {
  role: string;
  round: number;
  status: "finished" | "running" | "missing";
  outcome: OutcomeView;
  /** The record has this agent's finding for the round (else only trace events). */
  hasFinding: boolean;
  /**
   * Kept from a round a budget stop cut short (AgentFinding.partial, or the stop's
   * "partial findings kept" note): no verification ever read it.
   */
  partial: boolean;
  claims: ClaimView[];
  /** agent.finish claim texts, used while the record has no finding yet (untrusted). */
  claimTexts: string[];
  /** null: not recorded. */
  dropped: DroppedClaims[] | null;
  /** The verifier's open questions this agent was asked; null: not recorded. */
  questions: string[] | null;
  /** The evidence it recorded this round, in order. */
  evidence: EvidenceRef[];
  searches: RunbookSearch[];
  /** Runbook chunk IDs it saw (its citations); null: not recorded. */
  docRefs: string[] | null;
  /** Written by the agent, plus any notes RECAP added (format.ts splitSummary tells them apart). */
  summary: string;
  /** The notes RECAP recorded on this agent's model requests this round (llm.call `notes`). */
  modelNotes: string[];
  error: string | null;
  steps: number | null;
  toolCalls: number | null;
}

export interface DisagreementLine {
  kind: string;
  claims: string[];
  resolved: boolean;
  /** As recorded: may list several rules, comma-separated (see ruleIds). */
  ruleId: string;
  ruleIds: string[];
  /** RECAP's own sentence (trusted). */
  resolution: string;
}

/** The verifier's report right after a round (its verification.evidence event). */
export interface VerifierStep {
  status: string;
  summary: string;
  disagreements: DisagreementLine[];
  /** Its leads for the next round (RECAP's own text); [] when none or not recorded. */
  leads: string[];
}

/**
 * Which verification judged a round's claims: the last one that read them.
 * final: the run's final verification (record.verification) · earlier: the last verification
 * of an earlier epoch, before fix `fix` did not recover the system (RECAP then started over,
 * so later verifications never read these claims) · none: no verification read the round.
 */
export interface JudgedBy {
  kind: "final" | "earlier" | "none";
  /** earlier: the fix that ended the round's epoch (1 for the first). */
  fix: number | null;
}

export interface RoundView {
  round: number;
  routing: TraceEvent | undefined;
  orchestration: TraceEvent | undefined;
  mode: string;
  ruleId: string;
  reason: string;
  agents: AgentView[];
  /** The verifier's open questions this round was asked (trusted); null: not recorded. */
  questions: string[] | null;
  /** The verifier's leads this round was given (trusted RECAP text); [] when none or not recorded. */
  leads: string[];
  focusServices: string[];
  escalation: boolean;
  /**
   * A follow-up round for a conflict no evidence rule settled: the router saw the disagreement
   * signal, or the round was asked open questions. Whichever rule fired (a multi-service
   * incident routes by RT2-cross-service before RT3-disagreement is checked).
   */
  followUp: boolean;
  /**
   * Parallel rounds: true when the run records round-scoped views (ADR 0020: each specialist
   * saw what was known before the round plus its own results); null when it does not
   * (recorded before M3); false for single-agent rounds.
   */
  independent: boolean | null;
  verifier: VerifierStep | null;
  /** Epochs start at 0; each fix that did not recover the system starts the next one. */
  epoch: number;
  judgedBy: JudgedBy;
}

export interface RoundsInput {
  events: readonly TraceEvent[];
  record: RunRecord | null;
  /** The run's evidence registry (GET /runs/{id}); null while it has not loaded. */
  evidence: readonly Evidence[] | null;
  /** The run is still going: an agent without a finish is still working. */
  running: boolean;
}

interface AgentTrace {
  start?: TraceEvent;
  finish?: TraceEvent;
  calls: TraceEvent[];
  results: TraceEvent[];
  /** retrieval.docs events of this agent's own searches. */
  searches: TraceEvent[];
  /** agent.decision events: one per completed step. */
  decisions: number;
  /** Runbook chunk IDs in its context packs (context.pack items; a chunk ID is "stem#heading-n"). */
  packed: string[];
  /** RECAP's notes on its model requests (llm.call `notes`). */
  notes: string[];
}

interface RoundTrace {
  routing?: TraceEvent;
  investigation?: TraceEvent;
  orchestration?: TraceEvent;
  verification?: TraceEvent;
  agents: Map<string, AgentTrace>;
  /** The epoch at the round's routing.decision; undefined when no routing was traced. */
  epoch?: number;
  startSeq?: number;
  /** Agents whose findings a budget stop kept (orchestration.summary partial_findings_kept). */
  partialKept: string[];
}

function newAgentTrace(): AgentTrace {
  return { calls: [], results: [], searches: [], decisions: 0, packed: [], notes: [] };
}

function searchView(call: TraceEvent, trace: AgentTrace, running: boolean): RunbookSearch {
  const q = call.payload as ToolCallPayload;
  const args = fields(q.args);
  const id = text(q.invocation_id);
  const result = id
    ? trace.results.find((r) => text(r.payload?.invocation_id) === id)
    : trace.results.find((r) => r.seq > call.seq && r.payload?.tool === RUNBOOK_SEARCH_TOOL);
  const r = result?.payload as ToolResultPayload | undefined;
  // The handler emits retrieval.docs between the call and its result.
  const docs = trace.searches.find((s) => s.seq > call.seq && (!result || s.seq < result.seq));
  const hits = docs
    ? list<DocRef>((docs.payload as RetrievalPayload).results).map((d) => ({
        rank: rankLabel(d.rank),
        chunkId: text(d.chunk_id),
        docPath: text(d.doc_path),
        heading: text(d.heading),
        signals: signalViews(d.signals),
      }))
    : null;
  return {
    id: id || `seq-${call.seq}`,
    query: text(args.query),
    k: count(args.k),
    status: r ? (r.ok === false ? "failed" : "ok") : running ? "running" : "no_result",
    error: r?.error ? text(r.error) : null,
    evidenceId: r?.evidence_id ? text(r.evidence_id) : null,
    hits,
  };
}

function ruleIdList(ruleId: string): string[] {
  return unique(
    ruleId
      .split(",")
      .map((id) => id.trim())
      .filter(Boolean),
  );
}

/**
 * One view per investigation round, in round order, with one view per agent (in the
 * router's order). A round starts at its routing.decision; until the next one, tool calls,
 * runbook searches and the verifier's report belong to it.
 */
export function buildRounds({ events, record, evidence, running }: RoundsInput): RoundView[] {
  const rounds = new Map<number, RoundTrace>();
  const roundTrace = (n: number): RoundTrace => {
    let rt = rounds.get(n);
    if (!rt) {
      rt = { agents: new Map(), partialKept: [] };
      rounds.set(n, rt);
    }
    return rt;
  };
  const agentTrace = (rt: RoundTrace, role: string): AgentTrace => {
    let at = rt.agents.get(role);
    if (!at) {
      at = newAgentTrace();
      rt.agents.set(role, at);
    }
    return at;
  };

  const recordedBy = new Map<string, string>(); // evidence ID -> agent, from tool.result events
  // Every verification report (not a proposal check), with the epoch it belongs to.
  const reports: { seq: number; epoch: number; event: TraceEvent }[] = [];
  let epoch = 0;
  let current = 0;
  for (const e of [...events].sort((a, b) => a.seq - b.seq)) {
    const p = e.payload ?? {};
    switch (e.category) {
      case "routing.decision": {
        current = count((p as RoutingPayload).round) ?? current + 1;
        const rt = roundTrace(current);
        if (!rt.routing) {
          rt.routing = e;
          rt.epoch = epoch;
          rt.startSeq = e.seq;
        }
        break;
      }
      case "investigation.round":
        roundTrace(count(p.round) ?? current).investigation ??= e;
        break;
      case "agent.start":
      case "agent.finish": {
        const role = text(e.agent ?? p.role);
        if (!role) break;
        const at = agentTrace(roundTrace(count(p.round) ?? current), role);
        if (e.category === "agent.start") at.start ??= e;
        else at.finish = e;
        break;
      }
      case "agent.decision":
      case "context.pack":
      case "llm.call": {
        // The agent loop's own events: what a crashed agent did, and RECAP's notes on its answers.
        if (!e.agent || e.agent === "controller" || current === 0) break;
        const at = agentTrace(roundTrace(current), e.agent);
        if (e.category === "agent.decision") at.decisions += 1;
        else if (e.category === "context.pack") {
          for (const item of list((p as ContextPackPayload).items).map(text)) {
            if (item.includes("#") && !at.packed.includes(item)) at.packed.push(item);
          }
        } else at.notes.push(...list((p as LlmCallPayload).notes).map(text).filter(Boolean));
        break;
      }
      case "tool.call":
      case "tool.result": {
        const role = text(e.agent ?? p.caller);
        if (!role || role === "controller" || current === 0) break;
        const at = agentTrace(roundTrace(current), role);
        if (e.category === "tool.call") {
          at.calls.push(e);
        } else {
          at.results.push(e);
          const id = text((p as ToolResultPayload).evidence_id);
          if (id) recordedBy.set(id, role);
        }
        break;
      }
      case "retrieval.docs":
        if ((p as RetrievalPayload).stage === "agent" && e.agent && current) {
          agentTrace(roundTrace(current), e.agent).searches.push(e);
        }
        break;
      case "orchestration.summary": {
        const q = p as OrchestrationPayload;
        if (!q.mode) {
          // A budget stop's note: whose completed findings it kept (partial, never verified).
          if (current) roundTrace(current).partialKept.push(...list(q.partial_findings_kept).map(text));
          break;
        }
        roundTrace(count(q.round) ?? current).orchestration ??= e;
        break;
      }
      case "verification.evidence":
        // The first report after a round's agents; a proposal check is a different event.
        if (!(p as EvidenceCheckPayload).proposal_check) {
          if (current) roundTrace(current).verification ??= e;
          reports.push({ seq: e.seq, epoch, event: e });
        }
        break;
      case "verification.recovery":
        // A fix that did not recover the system: the controller starts over with a new epoch,
        // and later verifications read only the new epoch's findings.
        if ((p as RecoveryPayload).verified === false) epoch += 1;
        break;
      default:
        break;
    }
  }

  const findings = new Map<string, AgentFinding>();
  for (const f of list<AgentFinding>(record?.findings)) {
    const round = count(f.round) ?? 1;
    findings.set(`${f.agent_role}#${round}`, f);
    roundTrace(round);
  }

  const registry = evidence ? new Map(evidence.map((item) => [item.id, item])) : null;
  const resolve: Resolve = (id) => {
    const item = registry?.get(id);
    return {
      id,
      agent: item?.agent_role ?? recordedBy.get(id) ?? null,
      sourceRef: item?.source_ref ?? null,
      sourceType: item?.source_type ?? null,
      flags: item ? list(item.flags).map(text) : [],
      known: registry ? !!item : null,
    };
  };
  const finalChecks = record?.verification ? list<HypothesisCheck>(record.verification.checks) : null;
  const lastReport = reports[reports.length - 1];

  /** The verification that last read a round, and its checks (null: no verification read it). */
  const judgedFor = (rt: RoundTrace): { by: JudgedBy; checks: readonly HypothesisCheck[] | null } => {
    // No report in the trace (or a round no routing was traced for): only the record can tell.
    if (!lastReport || rt.epoch === undefined || rt.startSeq === undefined) {
      return { by: { kind: finalChecks ? "final" : "none", fix: null }, checks: finalChecks };
    }
    const start = rt.startSeq;
    const own = reports.filter((r) => r.epoch === rt.epoch && r.seq > start);
    const last = own[own.length - 1];
    if (!last) return { by: { kind: "none", fix: null }, checks: null };
    const payload = last.event.payload as EvidenceCheckPayload;
    if (last === lastReport) return { by: { kind: "final", fix: null }, checks: finalChecks ?? payloadChecks(payload) };
    return { by: { kind: "earlier", fix: last.epoch + 1 }, checks: payloadChecks(payload) };
  };

  return [...rounds.keys()]
    .sort((a, b) => a - b)
    .map((n) => {
      const rt = roundTrace(n);
      const routing = rt.routing?.payload as RoutingPayload | undefined;
      const orchestration = rt.orchestration?.payload as OrchestrationPayload | undefined;
      const mode = text(routing?.mode ?? orchestration?.mode) || "?";
      const ofRound = list<AgentFinding>(record?.findings)
        .filter((f) => (count(f.round) ?? 1) === n)
        .map((f) => f.agent_role);
      const roles = unique([...list(routing?.agents).map(text), ...rt.agents.keys(), ...ofRound]).filter(Boolean);
      // The orchestrator's own entry per agent: its error text when the agent timed out or crashed.
      const timeline = new Map(list<OrchestrationAgent>(orchestration?.agents).map((a) => [text(a.role), a]));
      const judged = judgedFor(rt);

      const agents = roles.map((role): AgentView => {
        const at = rt.agents.get(role) ?? newAgentTrace();
        const finding = findings.get(`${role}#${n}`);
        const start = at.start?.payload as AgentStartPayload | undefined;
        const finish = at.finish?.payload as AgentFinishPayload | undefined;
        const status = finding || finish ? "finished" : running ? "running" : "missing";
        const outcome =
          status === "running" ? RUNNING : status === "missing" ? MISSING : outcomeView(finding?.outcome || finish?.outcome || "");
        const evidenceIds = unique(
          [
            ...list(finding?.evidence_ids),
            ...list(finish?.evidence_ids),
            ...at.results.map((r) => (r.payload as ToolResultPayload).evidence_id),
          ]
            .map(text)
            .filter(Boolean),
        );
        const error = finding?.error || timeline.get(role)?.error;
        // The orchestrator's stand-in for an agent that timed out or crashed (no agent.finish):
        // its usage and citations are defaults, so what the agent did comes from the trace.
        const crashed = !finish && !!finding && (finding.outcome === "error" || !!finding.error);
        const partial = finding?.partial === true || rt.partialKept.includes(role);
        const verdict = (h: Hypothesis): FinalVerdict => {
          if (partial) return { kind: "unverified", reason: "partial" };
          if (judged.by.kind === "none") {
            return { kind: "unverified", reason: !lastReport ? "not_recorded" : running ? "pending" : "stopped" };
          }
          return finalVerdict(h, judged.checks, resolve);
        };
        return {
          role,
          round: n,
          status,
          outcome,
          hasFinding: !!finding,
          partial,
          claims: finding ? list<Hypothesis>(finding.hypotheses).map((h) => claimView(h, verdict, resolve)) : [],
          claimTexts: !finding && finish ? list(finish.hypotheses).map(text) : [],
          dropped: droppedClaims(finish),
          questions: Array.isArray(start?.questions) ? start.questions.map(text) : null,
          evidence: evidenceIds.map(resolve),
          searches: at.calls
            .filter((c) => isRunbookSearch(text((c.payload as ToolCallPayload).tool)))
            .map((c) => searchView(c, at, running)),
          docRefs: crashed ? at.packed : docRefsOf(finding),
          summary: text(finding?.summary || finish?.summary).trim(),
          modelNotes: unique(at.notes),
          error: error ? text(error) : null,
          steps: count(finish?.steps) ?? (crashed ? at.decisions : finding ? count(finding.usage?.steps) : null),
          toolCalls: count(finish?.tool_calls) ?? (crashed ? at.calls.length : finding ? count(finding.usage?.tool_calls) : null),
        };
      });

      const investigation = rt.investigation?.payload as InvestigationRoundPayload | undefined;
      const asked = agents.filter((a) => a.questions !== null);
      const questions = Array.isArray(investigation?.open_questions)
        ? investigation.open_questions.map(text)
        : asked.length
          ? unique(asked.flatMap((a) => a.questions ?? []))
          : null;

      const parallel = mode === "parallel";
      const scoped = agents.some((a) => a.evidence.some((ev) => isPerAgentId(ev.id)));
      const reported = agents.some((a) => a.status === "finished" && a.outcome.key !== "");

      const report = rt.verification?.payload as EvidenceCheckPayload | undefined;
      const verifier: VerifierStep | null = report
        ? {
            status: text(report.status) || "?",
            summary: text(report.summary),
            disagreements: list<Partial<Disagreement>>(report.disagreements).map((d) => ({
              kind: text(d.kind),
              claims: list(d.claims).map(text),
              resolved: d.resolved === true,
              ruleId: text(d.rule_id),
              ruleIds: ruleIdList(text(d.rule_id)),
              resolution: text(d.resolution),
            })),
            leads: unique(list(report.leads).map(text).filter(Boolean)),
          }
        : null;

      return {
        round: n,
        routing: rt.routing,
        orchestration: rt.orchestration,
        mode,
        ruleId: text(routing?.rule_id),
        reason: text(routing?.reason),
        agents,
        questions,
        leads: unique(list(investigation?.leads).map(text).filter(Boolean)),
        focusServices: list(investigation?.focus_services).map(text),
        escalation: investigation?.escalation === true,
        followUp: fields(routing?.signals).disagreement === true || (questions?.length ?? 0) > 0,
        independent: !parallel ? false : scoped || reported ? true : null,
        verifier,
        epoch: rt.epoch ?? 0,
        judgedBy: judged.by,
      };
    });
}

/**
 * A finding's citations. An M3 finding (it records an outcome) lists them even when empty; on
 * a run before M3 the API serves the missing list as [], which says nothing.
 */
function docRefsOf(finding: AgentFinding | undefined): string[] | null {
  if (!finding) return null;
  const refs = list(finding.doc_refs).map(text);
  return finding.outcome || refs.length ? refs : null;
}
