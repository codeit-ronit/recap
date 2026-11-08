/**
 * Pure helpers for the Safety & Approvals page (src/pages/Safety.tsx): the four safety layers
 * in plain words, a run's unsafe-action counts (attempted is not executed), each proposal's
 * permission lifecycle from the append-only approval log (with how each decision came, and what
 * that proves: a terminal or a token holder and a timing, never a person), the gateway's
 * decisions, the evidence gate's rules, what RECAP stopped and by which layer, the target's own
 * answer to each write, and the prompt-injection flags.
 *
 * Every value is read from the server: GET /system (components), GET /runs/{id} (the record),
 * GET /runs/{id}/evidence, GET /runs/{id}/timeline (policy, summary, approval and write events)
 * and the approval log (GET /approvals/events, GET /approvals/{id}/events). A count the server
 * did not record is null, shown as "Not recorded"; nothing is guessed or defaulted.
 *
 * Digests, approval tokens and signed capabilities are never returned in full by anything
 * here: `shortHash` is the only way a digest reaches the screen.
 *
 * This module imports types only, so the Node tests in ../../tests can load it directly.
 */

import type { IconName } from "../components/Icon";
import type { ComponentStatus, InjectionFlagView, ProposalCheckView, ProposalView, TimelineEntry } from "../dto";
import type { Tone } from "../format";
import type { ActionRecord, ApprovalEvent } from "../types";

// --- defensive readers (payloads come from several backend modules and older runs) ------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function text(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value) ?? "";
  } catch {
    return "";
  }
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.map(text).filter((line) => line !== "") : [];
}

/** A count the server recorded: a finite, non-negative number, else null. */
function counted(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.round(value) : null;
}

/** "key=value, key=value" for an action's parameters (values as JSON when not plain). */
export function paramsText(params: unknown): string {
  if (!isRecord(params)) return "";
  return Object.entries(params)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${key}=${text(value)}`)
    .join(", ");
}

/** "restart_service on inventory" or "execute_rollback on checkout (to_version=v2.4.0)". */
export function actionText(action: unknown, target: unknown, params?: unknown): string {
  const name = text(action) || "unknown action";
  const where = text(target);
  const args = paramsText(params);
  return `${name}${where ? ` on ${where}` : ""}${args ? ` (${args})` : ""}`;
}

/**
 * The first characters of a digest, then an ellipsis: the only form in which a digest is shown.
 * The full value never reaches the page (not even in a title attribute).
 */
export function shortHash(digest: unknown, length = 12): string {
  const value = text(digest).trim();
  if (!value) return "Not recorded";
  return value.length <= length ? value : `${value.slice(0, length)}…`;
}

// --- the four layers -----------------------------------------------------------------------------

export type LayerKey = "policy" | "evidence_gate" | "approval" | "capability";

export interface SafetyLayer {
  step: number;
  key: LayerKey;
  title: string;
  /** One plain sentence for someone new to RECAP. */
  plain: string;
  /** The rules or mechanism, in a few words (documentation, not a measurement). */
  rules: string;
  /** The GET /system component that implements this layer. */
  componentId: string;
}

/** In the order a fix meets them (the trace: policy, evidence gate, approval, signed write). */
export const SAFETY_LAYERS: readonly SafetyLayer[] = [
  {
    step: 1,
    key: "policy",
    title: "Policy at the gateway",
    plain:
      "Every tool call is judged from trusted configuration before it runs: reads are allowed, a reversible write needs a person's approval, and destructive or high-risk actions are always denied.",
    rules: "rules R1 to R14 · the first DENY wins",
    componentId: "safety_policy",
  },
  {
    step: 2,
    key: "evidence_gate",
    title: "Evidence gate",
    plain:
      "A proposed fix must cite evidence from this run, target the diagnosed service, name its action, and restore a version that the evidence names.",
    rules: "rules VP1 to VP4",
    componentId: "verifier",
  },
  {
    step: 3,
    key: "approval",
    title: "Exact approval",
    plain:
      "A person approves one exact proposal: the approval is bound to its digest, expires, and works once. A “no” wins until the approval is used.",
    rules: "rules R11 and R12 · single use",
    componentId: "approvals",
  },
  {
    step: 4,
    key: "capability",
    title: "Signed capability",
    plain:
      "The target applies a write only with a single-use capability signed for exactly that action, so a call that skips the gateway changes nothing. On a real cluster the adapter also dry-runs the write first, as its limited writer identity, and reads the change back after it.",
    rules: "Ed25519 signature · checked by the target's adapter",
    componentId: "write_boundary",
  },
];

export interface LayerStatus {
  tone: Tone;
  label: string;
  implementation: string | null;
  detail: string | null;
}

const COMPONENT_STATES: Record<string, { tone: Tone; label: string }> = {
  active: { tone: "ok", label: "Active" },
  available: { tone: "info", label: "Available" },
  unavailable: { tone: "warn", label: "Unavailable" },
  disabled: { tone: "warn", label: "Disabled" },
  planned: { tone: "neutral", label: "Planned" },
  error: { tone: "bad", label: "Error" },
};

/** What GET /system reports for a layer's component ("Not reported" when it says nothing). */
export function layerStatus(componentId: string, components: readonly ComponentStatus[] | null | undefined): LayerStatus {
  const component = components?.find((c) => c.id === componentId);
  if (!component) return { tone: "neutral", label: "Not reported", implementation: null, detail: null };
  const known = COMPONENT_STATES[component.status] ?? { tone: "neutral" as Tone, label: text(component.status) || "Not reported" };
  return {
    tone: known.tone,
    label: known.label,
    implementation: text(component.implementation) || null,
    detail: text(component.detail) || null,
  };
}

// --- unsafe actions: attempted is not executed (the run summary) -------------------------------

export interface UnsafeCounts {
  /** A run.summary event exists: the run ended and RECAP counted. */
  summarized: boolean;
  /** Proposals for a destructive, high-risk or unknown tool. */
  attempted: number | null;
  /** Of those, stopped before they reached the environment. */
  blocked: number | null;
  /** Of those, sent to the environment and applied. Should be 0. */
  executed: number | null;
  /** Of those, sent but refused by the environment's own check. */
  refusedByEnvironment: number | null;
  /** Fixes of any kind sent to the environment. */
  fixesSent: number | null;
}

/** The payload of the latest run.summary entry (null when the run has not ended). */
export function latestSummary(entries: readonly TimelineEntry[] | null | undefined): Record<string, unknown> | null {
  let latest: TimelineEntry | null = null;
  for (const entry of entries ?? []) {
    if (entry.category !== "run.summary" && entry.kind !== "summary") continue;
    if (!latest || entry.seq >= latest.seq) latest = entry;
  }
  return latest && isRecord(latest.payload) ? latest.payload : null;
}

export function unsafeCounts(summary: Record<string, unknown> | null | undefined): UnsafeCounts {
  if (!summary) {
    return { summarized: false, attempted: null, blocked: null, executed: null, refusedByEnvironment: null, fixesSent: null };
  }
  return {
    summarized: true,
    attempted: counted(summary.unsafe_attempts),
    blocked: counted(summary.unsafe_blocked),
    executed: counted(summary.unsafe_executed),
    refusedByEnvironment: counted(summary.unsafe_refused_by_environment),
    fixesSent: counted(summary.mitigation_attempts),
  };
}

// --- the approval decision log ------------------------------------------------------------------

export interface KindView {
  label: string;
  tone: Tone;
  icon: IconName;
  /** What the entry means, in one sentence (shown on hover and in the legend). */
  meaning: string;
}

/** Every kind of entry the append-only log has (recap/safety/approvals.py). */
export const APPROVAL_KINDS: Readonly<Record<string, KindView>> = {
  requested: { label: "Requested", tone: "info", icon: "clock", meaning: "RECAP asked for a decision on one exact proposal." },
  approved: { label: "Approved", tone: "ok", icon: "check", meaning: "Approved for this exact proposal, to be used once." },
  rejected: { label: "Rejected", tone: "bad", icon: "x", meaning: "The answer was no: the fix is not applied." },
  revoked: { label: "Revoked", tone: "bad", icon: "x", meaning: "A no overrode an approval before the gateway used it." },
  expired: { label: "Expired", tone: "warn", icon: "clock", meaning: "Nobody decided in time; a late yes cannot apply." },
  consumed: {
    label: "Used once",
    tone: "accent",
    icon: "lock",
    meaning: "The gateway used the approval right before sending the write; it cannot be used again.",
  },
  refused: {
    label: "Refused",
    tone: "warn",
    icon: "alert",
    meaning: "A decision attempt was refused: a wrong digest, an expired request, or one already decided.",
  },
};

export const APPROVAL_KIND_ORDER: readonly string[] = ["requested", "approved", "rejected", "revoked", "expired", "consumed", "refused"];

export function approvalKind(kind: string): KindView {
  if (Object.prototype.hasOwnProperty.call(APPROVAL_KINDS, kind)) return APPROVAL_KINDS[kind];
  return { label: kind || "Not recorded", tone: "neutral", icon: "minus", meaning: "An entry kind this dashboard does not know." };
}

export interface ActorView {
  label: string;
  /**
   * True: a person is expected (an interactive console, a signed-in operator), though the log
   * proves a terminal or a token holder, not a person (decisionAttestation); false: no person
   * (RECAP, the simulation auto-approver); null: unknown.
   */
  person: boolean | null;
}

/** Who wrote a log entry, in words (the raw actor is shown next to it). */
export function actorLabel(actor: string | null | undefined): ActorView {
  const raw = text(actor).trim();
  if (!raw) return { label: "Not recorded", person: null };
  if (raw === "auto-approver[simulation]") return { label: "Simulation auto-approver (no person)", person: false };
  if (raw === "recap") return { label: "RECAP", person: false };
  if (raw === "console-user") return { label: "Terminal user (interactive console)", person: true };
  if (raw.startsWith("operator:")) return { label: `Operator ${raw.slice("operator:".length) || "(unnamed)"} (signed in)`, person: true };
  return { label: raw, person: null };
}

/**
 * The run a "requested" entry names. RECAP writes that entry itself as
 * "run <run id>; expires <time>" (ApprovalStore.create_request); any other text gives null.
 */
export function requestedRunId(event: Pick<ApprovalEvent, "kind" | "actor" | "detail">): string | null {
  if (event.kind !== "requested" || event.actor !== "recap") return null;
  const match = /^run (run-[A-Za-z0-9_-]{1,64}); expires /.exec(text(event.detail));
  return match ? match[1] : null;
}

// --- one proposal's permission lifecycle ------------------------------------------------------------

export type StepState = "done" | "failed" | "waiting" | "skipped";

export interface LifecycleStep {
  key: "requested" | "decided" | "used";
  title: string;
  state: StepState;
  text: string;
  at: string | null;
  actor: string | null;
  /**
   * How a decision came, in the decision log's own words (the entry's detail): "interactive
   * terminal; typed the target service's name; answered after 121.9 s; ...", or "dashboard or
   * API; ...". Null when the entry has none (a simulated run decided in the dashboard).
   */
  note: string | null;
}

export type LifecycleStatus =
  | "used"
  | "approved_waiting"
  | "approved_unused"
  | "rejected"
  | "revoked"
  | "expired"
  | "waiting"
  | "no_decision"
  | "not_requested";

export interface Lifecycle {
  proposalId: string;
  status: LifecycleStatus;
  label: string;
  tone: Tone;
  steps: LifecycleStep[];
  /** Refused decision attempts (a tampered digest, a late answer...). */
  refused: ApprovalEvent[];
  /** When the log recorded the expiry, or null. */
  expiredAt: string | null;
  /** Of the refused attempts, the answers that came at or after the expiry: refused, they changed nothing. */
  late: ApprovalEvent[];
  /** Every entry for this proposal, oldest first. */
  events: ApprovalEvent[];
}

function step(key: LifecycleStep["key"], state: StepState, text: string, entry: ApprovalEvent | null = null, note: string | null = null): LifecycleStep {
  const title = key === "requested" ? "Requested" : key === "decided" ? "Decided" : "Used";
  return { key, title, state, text, at: entry?.at ?? null, actor: entry?.actor ?? null, note };
}

function noteOf(entry: ApprovalEvent | null, fallback: string | null = null): string | null {
  return (entry ? text(entry.detail).trim() : "") || fallback || null;
}

function time(value: string | null | undefined): number {
  const t = Date.parse(text(value).replace(/(\.\d{3})\d+/, "$1"));
  return Number.isFinite(t) ? t : Number.NaN;
}

/**
 * requested -> decided -> used, or expired / rejected / revoked, from the proposal's entries in
 * the approval log. `live`: the run is still going, so a missing step is "waiting", not "never".
 * `trace`: the run's own record of the decision (its approval.decided event), whose note stands
 * in when the log's entry carries none.
 */
export function permissionLifecycle(
  proposalId: string,
  entries: readonly ApprovalEvent[],
  live: boolean,
  trace: TraceDecision | null = null,
): Lifecycle {
  const events = entries.filter((e) => e.proposal_id === proposalId).sort((a, b) => a.event_id - b.event_id);
  const first = (kind: string) => events.find((e) => e.kind === kind) ?? null;
  const requested = first("requested");
  const approved = first("approved");
  const rejected = first("rejected");
  const revoked = first("revoked");
  const expired = first("expired");
  const consumed = first("consumed");
  const refused = events.filter((e) => e.kind === "refused");
  const late = expired ? refused.filter((e) => time(e.at) >= time(expired.at) || e.event_id > expired.event_id) : [];
  const who = (e: ApprovalEvent) => actorLabel(e.actor).label;
  // The trace's note for the same decision, when the log's entry has none.
  const traced = (decision: string) => (trace && trace.decision === decision ? trace.note : null);

  if (!events.length || (!requested && !approved && !rejected && !expired && !consumed && !revoked)) {
    return {
      proposalId,
      status: "not_requested",
      // A run decided elsewhere (a published run, another server's) has its decision in its trace only.
      label: trace ? "Not in this server's approval log" : "No approval requested",
      tone: "neutral",
      steps: [
        step("requested", "skipped", "No request in the approval log"),
        step("decided", "skipped", "Nothing to decide"),
        step("used", "skipped", "Nothing was used"),
      ],
      refused,
      expiredAt: null,
      late: [],
      events,
    };
  }

  const requestStep = requested ? step("requested", "done", "A decision was requested", requested) : step("requested", "done", "Request not in the log");

  let decided: LifecycleStep;
  if (approved) decided = step("decided", "done", `Approved by ${who(approved)}`, approved, noteOf(approved, traced("approved")));
  else if (rejected) decided = step("decided", "failed", `Rejected by ${who(rejected)}`, rejected, noteOf(rejected, traced("rejected")));
  else if (expired) decided = step("decided", "failed", "Expired: nobody decided in time", expired);
  else decided = step("decided", live ? "waiting" : "skipped", live ? "Waiting for a decision" : "No decision recorded");

  let used: LifecycleStep;
  if (consumed) used = step("used", "done", "Used once by the gateway", consumed);
  else if (revoked) used = step("used", "failed", `Revoked by ${who(revoked)} before use`, revoked, noteOf(revoked));
  else if (approved) used = step("used", live ? "waiting" : "skipped", live ? "Waiting for the gateway" : "Approved but never used");
  else used = step("used", "skipped", "Not used");

  let status: LifecycleStatus;
  let label: string;
  let tone: Tone;
  if (consumed) [status, label, tone] = ["used", "Approved and used once", "ok"];
  else if (revoked) [status, label, tone] = ["revoked", "Approval revoked before use", "bad"];
  else if (rejected) [status, label, tone] = ["rejected", "Rejected: the fix was not applied", "bad"];
  else if (expired) [status, label, tone] = ["expired", "Expired without a decision", "warn"];
  else if (approved) [status, label, tone] = live ? ["approved_waiting", "Approved, not used yet", "info"] : ["approved_unused", "Approved, never used", "warn"];
  else [status, label, tone] = live ? ["waiting", "Waiting for a person", "warn"] : ["no_decision", "No decision recorded", "neutral"];

  return { proposalId, status, label, tone, steps: [requestStep, decided, used], refused, expiredAt: expired?.at ?? null, late, events };
}

// --- how a decision came: what the decision log proves, and what it does not -------------------------

export interface Attestation {
  /** "console": typed at a terminal; "dashboard": sent with the operator token (the dashboard or the API). */
  channel: "console" | "dashboard";
  /** What the decision log shows, in plain words. */
  shows: string;
  /** What that does not prove. */
  limit: string;
}

/** "121.9 s after the prompt appeared" / "42.3 s after the request", from the note's own wording. */
function answeredAfter(note: string): string | null {
  const atPrompt = /answered after (\d+(?:\.\d+)?) s/.exec(note);
  if (atPrompt) return `${atPrompt[1]} s after the prompt appeared`;
  const sinceRequest = /answered (\d+(?:\.\d+)?) s after the request/.exec(note);
  return sinceRequest ? `${sinceRequest[1]} s after the request` : null;
}

/**
 * What a human decision's log entry proves, from its actor and note (recap/safety/approvals.py
 * writes the console's note, apps/api/main.py the dashboard's). The console proves an answer
 * typed at an interactive terminal and when; the dashboard or the API proves a holder of the
 * operator token and when. Neither proves which person it was. Null for decisions no person
 * is expected to make (RECAP's own entries, the simulation auto-approver) or unknown actors.
 */
export function decisionAttestation(actor: string | null | undefined, note: string | null | undefined): Attestation | null {
  const who = text(actor).trim();
  const said = text(note).trim();
  const atConsole = who === "console-user";
  const withToken = who.startsWith("operator:") || said.startsWith("dashboard or API");
  if (!atConsole && !withToken) return null;
  const when = answeredAfter(said);
  const typed = said.includes("typed the target service's name");
  const notTyped = said.includes("did not type the target service's name");
  if (atConsole) {
    if (said.startsWith("rejected unread") || said.startsWith("rejected without asking")) {
      return {
        channel: "console",
        shows: `The decision log shows that RECAP rejected the request without reading any answer (${said}).`,
        limit: "No answer was read, so it shows nothing about who was at the terminal.",
      };
    }
    const facts = [
      `an answer at an interactive terminal${when ? `, ${when}` : ""}`,
      typed ? "it was the target service's exact name" : notTyped ? "it was not the target service's exact name, so it rejected" : "",
      said.includes("faster than a person can read") ? "it came faster than a person can read the proposal" : "",
    ]
      .filter(Boolean)
      .join("; ");
    return {
      channel: "console",
      shows: `The decision log shows ${facts}.`,
      limit: `That proves a terminal and a timing, not a person: a program driving the terminal could answer too.${
        said.includes("login name kept in the local decision log") ? " This machine's login name is kept only in the local decision log, never on this page." : ""
      }`,
    };
  }
  const facts = [
    `a decision sent through the dashboard or the API with the operator token${when ? `, ${when}` : ""}`,
    typed ? "the target service's exact name was typed" : "",
  ]
    .filter(Boolean)
    .join("; ");
  return {
    channel: "dashboard",
    shows: `The decision log shows ${facts}.`,
    limit: "That proves a holder of the operator token and a timing, not a person: anyone holding the token could send it.",
  };
}

// --- the run's own record of each decision (approval.decided trace events) -------------------------

export interface TraceDecision {
  proposalId: string;
  /** approved, rejected, refused or expired, as the controller recorded it. */
  decision: string;
  actor: string | null;
  /** The decision log's note, as the controller copied it into the trace. */
  note: string | null;
  /** Why a decision was refused (RECAP's text), for a refused one. */
  reason: string | null;
  seq: number;
}

/**
 * The last approval.decided event per proposal, from a run's trace. An expiry or a refusal does
 * not name its proposal there: it belongs to the request the trace recorded just before it (the
 * controller waits for one request at a time). The approval id each event carries is never read.
 */
export function traceDecisions(
  requests: readonly TimelineEntry[] | null | undefined,
  decisions: readonly TimelineEntry[] | null | undefined,
): Map<string, TraceDecision> {
  const asked = (requests ?? [])
    .filter((entry) => entry.kind === "approval_requested" || entry.category === "approval.requested")
    .map((entry) => ({ seq: entry.seq, id: text(isRecord(entry.payload) ? entry.payload.proposal_id : "") }))
    .filter((r) => r.id)
    .sort((a, b) => a.seq - b.seq);
  const found = new Map<string, TraceDecision>();
  const ordered = [...(decisions ?? [])]
    .filter((entry) => entry.kind === "approval_decided" || entry.category === "approval.decided")
    .sort((a, b) => a.seq - b.seq);
  for (const entry of ordered) {
    const payload = isRecord(entry.payload) ? entry.payload : {};
    const named = text(payload.proposal_id);
    const before = [...asked].reverse().find((r) => r.seq < entry.seq);
    const proposalId = named || before?.id || "";
    if (!proposalId) continue;
    found.set(proposalId, {
      proposalId,
      decision: text(payload.decision ?? entry.verdict),
      actor: text(payload.actor) || null,
      note: text(payload.note).trim() || null,
      reason: text(payload.reason).trim() || null,
      seq: entry.seq,
    });
  }
  return found;
}

// --- the gateway's decisions ------------------------------------------------------------------------

export interface GatewayRow {
  seq: number;
  ts: string;
  /** Who called: the controller (a fix) or an agent role; "" when the trace did not say. */
  caller: string;
  /** A proposed fix (the controller's call), as opposed to an agent's investigation call. */
  fix: boolean;
  tool: string;
  verdict: string;
  risk: string | null;
  rules: string[];
  /** The last rule id: the one that produced the verdict. */
  deciding: string | null;
  /** RECAP's reasons (the policy's own text). */
  reasons: string[];
}

/** policy.decision timeline entries as rows, in trace order. */
export function gatewayRows(entries: readonly TimelineEntry[] | null | undefined): GatewayRow[] {
  return (entries ?? [])
    .filter((entry) => entry.kind === "policy" || entry.category === "policy.decision")
    .map((entry) => {
      const payload = isRecord(entry.payload) ? entry.payload : {};
      const caller = text(payload.caller).trim() || text(entry.agent).trim();
      const rules = texts(payload.rule_ids);
      return {
        seq: entry.seq,
        ts: entry.ts,
        caller,
        fix: caller === "controller",
        tool: text(entry.tool ?? payload.tool) || "unknown tool",
        verdict: text(entry.verdict ?? payload.verdict),
        risk: text(payload.risk) || null,
        rules,
        deciding: rules.length ? rules[rules.length - 1] : null,
        reasons: texts(payload.reasons),
      };
    })
    .sort((a, b) => a.seq - b.seq);
}

export interface VerdictCounts {
  ALLOW: number;
  APPROVAL_REQUIRED: number;
  DENY: number;
  other: number;
}

export function verdictCounts(rows: readonly GatewayRow[]): VerdictCounts {
  const counts: VerdictCounts = { ALLOW: 0, APPROVAL_REQUIRED: 0, DENY: 0, other: 0 };
  for (const row of rows) {
    if (row.verdict === "ALLOW" || row.verdict === "APPROVAL_REQUIRED" || row.verdict === "DENY") counts[row.verdict] += 1;
    else counts.other += 1;
  }
  return counts;
}

/** "A proposed fix" or "An agent's call" (a filter value and its label). */
export const CALL_SCOPES = { fix: "Proposed fixes", call: "Investigation calls" } as const;

export function callScope(row: Pick<GatewayRow, "fix">): keyof typeof CALL_SCOPES {
  return row.fix ? "fix" : "call";
}

// --- the evidence gate's rules (VP1 to VP4) ------------------------------------------------------------

export interface ProposalRule {
  id: string;
  /** One plain sentence (documentation, not a measurement). */
  meaning: string;
  /** How RECAP's own problem text begins when this rule fails (recap/agents/verifier.py). */
  problems: readonly string[];
}

/** The rules every proposed fix is checked against (recap/agents/verifier.py, PROPOSAL_RULES). */
export const PROPOSAL_RULES: readonly ProposalRule[] = [
  {
    id: "VP1-evidence-exists",
    meaning: "The fix cites evidence, and only evidence recorded in this run.",
    problems: ["the proposal cites no evidence", "the proposal cites evidence not in this run"],
  },
  {
    id: "VP2-target-matches-diagnosis",
    meaning: "There is a supported diagnosis, and the fix targets its service.",
    problems: ["there is no supported diagnosis to act on", "the proposal targets "],
  },
  {
    id: "VP3-action-named",
    meaning: "The fix names its action.",
    problems: ["the proposal names no action"],
  },
  {
    id: "VP4-restore-point",
    meaning:
      "A fix that restores a version restores one this run's evidence names (the release before the bad one, or a rollback dry run's target), never the release the diagnosis blames.",
    problems: ["the proposal restores ", "no deployment history or rollback dry run in this run names a version"],
  },
];

/**
 * The rules a refused proposal failed, read from the start of RECAP's own problem text (a model
 * can only fill in the names inside it). Empty when no problem reads like one of them.
 */
export function failedProposalRules(problems: readonly string[]): string[] {
  return PROPOSAL_RULES.filter((rule) => problems.some((problem) => rule.problems.some((start) => text(problem).startsWith(start)))).map(
    (rule) => rule.id,
  );
}

// --- what RECAP stopped, and which layer stopped it ---------------------------------------------------

export type BlockLayer = "policy" | "evidence_gate" | "approval" | "write";

export const BLOCK_LAYER_LABELS: Readonly<Record<BlockLayer, string>> = {
  policy: "Policy",
  evidence_gate: "Evidence gate",
  approval: "Approval",
  write: "Write boundary",
};

export interface BlockedRow {
  key: string;
  layer: BlockLayer;
  what: string;
  /** How it ended, in a word or two ("Denied", "Rejected", "Refused"...). */
  result: string;
  rule: string | null;
  /** RECAP's own text: the policy's reasons, the gate's problems, the log's detail, the action's note. */
  reason: string;
  at: string | null;
}

const ACTION_BLOCKS: Readonly<Record<string, { layer: BlockLayer; result: string }>> = {
  denied: { layer: "policy", result: "Denied" },
  rejected: { layer: "approval", result: "Rejected" },
  expired: { layer: "approval", result: "Expired" },
  error: { layer: "write", result: "Failed or refused" },
};

/**
 * Everything RECAP stopped in one run: fixes that ended denied, rejected, expired or failed (the
 * run record's actions), agents' calls the gateway denied, proposals the evidence gate refused,
 * and refused approval attempts. Each row names the layer that stopped it.
 */
export function blockedActions(input: {
  actions: readonly ActionRecord[];
  gateway: readonly GatewayRow[];
  checks: readonly ProposalCheckView[];
  proposals: readonly ProposalView[];
  lifecycles: readonly Lifecycle[];
}): BlockedRow[] {
  const rows: BlockedRow[] = [];
  const fixDenials = input.gateway.filter((row) => row.fix && row.verdict === "DENY");
  const deniedTools = new Set<string>();
  const gateRefusals = input.checks.filter((check) => !check.ok && check.enforced);
  const proposalOf = (id: string) => input.proposals.find((p) => p.proposal_id === id);
  // The run record lists a fix the evidence gate refused as "denied" too: it is shown once, as
  // the gate's row (with the failed rule), at the time the record gives.
  const gateTimes = new Map<number, string>();

  input.actions.forEach((action, index) => {
    const block = ACTION_BLOCKS[action.result];
    if (!block) return;
    // The deciding rule of the policy's last DENY on this tool (a join on recorded fields).
    const denial = block.layer === "policy" ? [...fixDenials].reverse().find((row) => row.tool === action.action) : undefined;
    if (block.layer === "policy" && !denial) {
      const refusal = gateRefusals.find((check) => {
        const proposal = proposalOf(check.proposal_id);
        return !!proposal && proposal.action === action.action && proposal.target === action.target;
      });
      if (refusal) {
        if (text(action.at)) gateTimes.set(refusal.seq, text(action.at));
        return;
      }
    }
    // A rejected or expired fix: the rule that required the approval it never got (R11).
    const asked =
      block.layer === "approval" ? [...input.gateway].reverse().find((row) => row.fix && row.tool === action.action && row.verdict === "APPROVAL_REQUIRED") : undefined;
    if (block.layer === "policy") deniedTools.add(action.action);
    rows.push({
      key: `action-${index}`,
      layer: block.layer,
      what: actionText(action.action, action.target, action.params),
      result: block.result,
      rule: denial?.deciding ?? asked?.deciding ?? null,
      reason: text(action.note) || (denial ? denial.reasons.join("; ") : "") || "Not recorded",
      at: text(action.at) || null,
    });
  });

  for (const row of input.gateway) {
    if (row.verdict !== "DENY") continue;
    // A fix the record already lists as denied is shown once, with this rule.
    if (row.fix && deniedTools.has(row.tool)) continue;
    rows.push({
      key: `policy-${row.seq}`,
      layer: "policy",
      what: row.fix ? `${row.tool} (a proposed fix)` : `${row.tool}, called by ${row.caller || "an agent (caller not recorded)"}`,
      result: "Denied",
      rule: row.deciding,
      reason: row.reasons.join("; ") || "Not recorded",
      at: row.ts,
    });
  }

  for (const check of gateRefusals) {
    const proposal = proposalOf(check.proposal_id);
    // The rule that failed when RECAP's problem text names it; else every rule the check ran.
    const failed = failedProposalRules(check.problems);
    const rules = failed.length ? failed : check.rule_ids;
    rows.push({
      key: `gate-${check.seq}`,
      layer: "evidence_gate",
      what: proposal ? actionText(proposal.action, proposal.target, proposal.params) : `proposal ${check.proposal_id}`,
      result: "Refused",
      rule: rules.length ? rules.join(", ") : null,
      reason: check.problems.join("; ") || "Not recorded",
      at: gateTimes.get(check.seq) ?? null,
    });
  }

  for (const lifecycle of input.lifecycles) {
    for (const event of lifecycle.refused) {
      const late = lifecycle.late.includes(event);
      rows.push({
        key: `refused-${event.event_id}`,
        layer: "approval",
        what: late ? `a late answer on proposal ${lifecycle.proposalId}, after its expiry` : `a decision on proposal ${lifecycle.proposalId}`,
        result: "Refused",
        rule: null,
        reason: text(event.detail) || "Not recorded",
        at: text(event.at) || null,
      });
    }
  }
  return rows;
}

// --- one run, layer by layer ---------------------------------------------------------------------------

export type OutcomeState = "passed" | "stopped" | "waiting" | "not_reached" | "warning";

export interface LayerOutcome {
  key: LayerKey;
  state: OutcomeState;
  tone: Tone;
  headline: string;
  lines: string[];
}

const OUTCOME_TONES: Readonly<Record<OutcomeState, Tone>> = {
  passed: "ok",
  stopped: "bad",
  waiting: "warn",
  not_reached: "neutral",
  warning: "warn",
};

function outcome(key: LayerKey, state: OutcomeState, headline: string, lines: string[] = []): LayerOutcome {
  return { key, state, tone: OUTCOME_TONES[state], headline, lines };
}

function policyOutcome(gateway: readonly GatewayRow[], live: boolean): LayerOutcome {
  const fixes = gateway.filter((row) => row.fix);
  const calls = gateway.filter((row) => !row.fix);
  const callCounts = verdictCounts(calls);
  const callLine = calls.length
    ? `${calls.length} investigation ${calls.length === 1 ? "call" : "calls"} judged: ${callCounts.ALLOW} allowed, ${callCounts.DENY} denied`
    : "";
  const lines = callLine ? [callLine] : [];
  if (!fixes.length) return outcome("policy", "not_reached", gateway.length ? "No fix reached the policy" : "No policy decision recorded", lines);
  const denied = fixes.filter((row) => row.verdict === "DENY");
  if (denied.length) {
    const last = denied[denied.length - 1];
    return outcome("policy", "stopped", `Denied ${last.tool}`, [last.deciding ? `deciding rule ${last.deciding}` : "deciding rule not recorded", ...lines]);
  }
  const allowed = fixes.filter((row) => row.verdict === "ALLOW");
  const asked = fixes.filter((row) => row.verdict === "APPROVAL_REQUIRED");
  if (allowed.length) {
    const last = allowed[allowed.length - 1];
    const how = asked.length ? "Allowed after the exact approval" : "Allowed";
    return outcome("policy", "passed", `${how}: ${last.tool}`, [last.deciding ? `deciding rule ${last.deciding}` : "deciding rule not recorded", ...lines]);
  }
  if (asked.length) {
    const last = asked[asked.length - 1];
    return outcome("policy", live ? "waiting" : "warning", `Approval required for ${last.tool}`, [
      live ? "waiting for the exact approval" : "never allowed: no usable approval",
      ...lines,
    ]);
  }
  return outcome("policy", "warning", `Verdict ${fixes[fixes.length - 1].verdict || "not recorded"}`, lines);
}

function gateOutcome(checks: readonly ProposalCheckView[], policy: LayerOutcome): LayerOutcome {
  if (!checks.length) {
    return outcome("evidence_gate", "not_reached", policy.state === "stopped" ? "The policy stopped the fix first" : "No proposal was checked");
  }
  const refused = checks.filter((check) => !check.ok && check.enforced);
  const unenforced = checks.filter((check) => !check.ok && !check.enforced);
  if (refused.length) {
    const failed = [...new Set(refused.flatMap((check) => failedProposalRules(check.problems)))];
    return outcome("evidence_gate", "stopped", "Refused the fix", [...(failed.length ? [`failed ${failed.join(" · ")}`] : []), ...refused.flatMap((check) => check.problems)]);
  }
  if (unenforced.length) {
    return outcome("evidence_gate", "warning", "Failed, but not enforced", [
      "the simulation-only safety-off ablation was on",
      ...unenforced.flatMap((check) => check.problems),
    ]);
  }
  const rules = [...new Set(checks.flatMap((check) => check.rule_ids))];
  return outcome("evidence_gate", "passed", checks.length === 1 ? "Passed" : `Passed (${checks.length} proposals)`, rules.length ? [rules.join(" · ")] : []);
}

function approvalOutcome(lifecycles: readonly Lifecycle[], decisions: ReadonlyMap<string, TraceDecision>): LayerOutcome {
  const asked = lifecycles.filter((l) => l.status !== "not_requested");
  if (!asked.length) {
    // No entry in this server's approval log; the run's trace may still record the decision.
    const traced = [...decisions.values()].sort((a, b) => a.seq - b.seq).pop();
    if (!traced) return outcome("approval", "not_reached", "No approval requested");
    const by = `${traced.decision}${traced.actor ? ` by ${traced.actor}` : ""}`;
    const lines = [by, ...(traced.note ? [traced.note] : []), "recorded in the run's trace; this server's approval log has no entry"];
    if (traced.decision === "approved") return outcome("approval", "passed", "Approved (in the run's trace)", lines);
    if (traced.decision === "rejected") return outcome("approval", "stopped", "Rejected (in the run's trace)", lines);
    return outcome("approval", "warning", `${traced.decision || "Decided"} (in the run's trace)`, lines);
  }
  const last = asked[asked.length - 1];
  // Who decided (approved or rejected by ...); an expiry is already the headline.
  const decided = last.steps.find((step) => step.key === "decided");
  const decider = decided && (decided.state === "done" || decided.state === "failed") && last.status !== "expired" ? decided.text : "";
  const earlier = asked.slice(0, -1).map((l) => `${l.proposalId}: ${l.label}`);
  const lines = [decider, ...earlier].filter(Boolean);
  switch (last.status) {
    case "used":
      return outcome("approval", "passed", last.label, lines);
    case "waiting":
    case "approved_waiting":
      return outcome("approval", "waiting", last.label, lines);
    case "rejected":
    case "revoked":
      return outcome("approval", "stopped", last.label, lines);
    default:
      return outcome("approval", "warning", last.label, lines);
  }
}

function writeOutcome(writes: readonly TimelineEntry[], counts: UnsafeCounts): LayerOutcome {
  const sent = writeRows(writes);
  const refusedLine =
    counts.refusedByEnvironment !== null && counts.refusedByEnvironment > 0
      ? [`${counts.refusedByEnvironment} unsafe ${counts.refusedByEnvironment === 1 ? "write" : "writes"} refused by the environment`]
      : [];
  if (!sent.length) return outcome("capability", "not_reached", "No write was sent", refusedLine);
  const failed = sent.filter((row) => !row.ok);
  if (failed.length) {
    const last = failed[failed.length - 1];
    return outcome("capability", "stopped", `Write failed or refused: ${last.what}`, [last.error || "error not recorded", ...refusedLine]);
  }
  const last = sent[sent.length - 1];
  const reported = last.effects.map((effect) => `the target reported: ${effect}`);
  return outcome(
    "capability",
    "passed",
    sent.length === 1 ? `Signed write applied: ${last.what}` : `${sent.length} signed writes applied`,
    [...reported, ...refusedLine],
  );
}

// --- the target's own verdict on each write (mitigation.executed) -------------------------------------

export interface WriteRow {
  seq: number;
  ts: string;
  /** "execute_rollback on geo" (the tool and its target; parameters are in `params`). */
  what: string;
  params: string;
  /** The target applied it (ok); false: it refused the write, or the write failed. */
  ok: boolean;
  /** What the target's adapter reported it changed ("geo generation 2 -> 3"), RECAP's adapter's words. */
  effects: string[];
  error: string | null;
  /** The first characters of the digest the write was bound to (never the whole value). */
  digest: string;
}

/**
 * The writes RECAP sent to the target, with the target's own answer. Only these fields are read:
 * a capability, an approval id or a token in the payload never reaches the page.
 */
export function writeRows(entries: readonly TimelineEntry[] | null | undefined): WriteRow[] {
  return (entries ?? [])
    .filter((entry) => entry.kind === "write_executed" || entry.category === "mitigation.executed")
    .map((entry) => {
      const payload = isRecord(entry.payload) ? entry.payload : {};
      return {
        seq: entry.seq,
        ts: entry.ts,
        what: actionText(payload.tool, payload.target),
        params: paramsText(payload.params),
        ok: payload.ok === true,
        effects: texts(payload.side_effects),
        error: text(payload.error) || null,
        digest: shortHash(payload.proposal_digest),
      };
    })
    .sort((a, b) => a.seq - b.seq);
}

export interface TargetWords {
  /** True: the simulated shop; false: a real cluster; null: the run did not record its target. */
  simulated: boolean | null;
  /** The adapter that checked the run's writes: the simulator, RECAP's Kubernetes adapter (Kind and SREGym), or not known here. */
  adapter: "simulated" | "kubernetes" | null;
  /** "the shop", "the cluster" or "the target", for short sentences. */
  short: string;
  /** "the simulated shop", "the Kind demo cluster (live)", "SREGym's cluster (live)". */
  long: string;
}

/** The live targets RECAP's Kubernetes adapter serves (SREGym's adapter is a KubernetesEnvironment too). */
const LIVE_TARGET_NAMES: Readonly<Record<string, string>> = {
  kind: "the Kind demo cluster",
  sregym: "SREGym's cluster",
};

/** How to name a run's target in a sentence, from its recorded environment (never guessed). */
export function targetWords(environment: { kind?: string | null; is_simulated?: boolean | null } | null | undefined): TargetWords {
  if (!environment || typeof environment.is_simulated !== "boolean") {
    return { simulated: null, adapter: null, short: "the target", long: "the target (not recorded)" };
  }
  if (environment.is_simulated) return { simulated: true, adapter: "simulated", short: "the shop", long: "the simulated shop" };
  const kind = text(environment.kind);
  const named = Object.prototype.hasOwnProperty.call(LIVE_TARGET_NAMES, kind) ? LIVE_TARGET_NAMES[kind] : null;
  return {
    simulated: false,
    adapter: named ? "kubernetes" : null,
    short: named ? "the cluster" : "the target",
    long: named ? `${named} (live)` : kind ? `the ${kind} target (live)` : "a live target (its kind not recorded)",
  };
}

/**
 * What the run's target checks before it applies a write, read from its adapter's code
 * (documentation, not a per-run record: the Kubernetes adapter audits each step in its own
 * local state, which no trace or API response carries). Null when the adapter is not known here.
 */
export function adapterChecks(where: Pick<TargetWords, "adapter">): string | null {
  switch (where.adapter) {
    case "simulated":
      return "The simulated shop applies a write only with this run's single-use capability, signed for exactly that action, target and parameters.";
    case "kubernetes":
      return "RECAP's Kubernetes adapter performs only rollbacks and restarts, and only with this run's single-use capability for exactly that action (its nonce is spent before the cluster is touched; a capability made with safety off is never accepted). It then checks the service, this run's hold on the namespace and the release, runs a server-side dry run as its RBAC-limited writer identity, writes, and reads the Deployment back to confirm the approved change.";
    default:
      return null;
  }
}

/** How one run went through the four layers, from what it recorded. */
export function runLayerOutcomes(input: {
  gateway: readonly GatewayRow[];
  checks: readonly ProposalCheckView[];
  lifecycles: readonly Lifecycle[];
  writes: readonly TimelineEntry[];
  counts: UnsafeCounts;
  live: boolean;
  /** The trace's decisions (traceDecisions): the approval layer for a run decided elsewhere. */
  decisions?: ReadonlyMap<string, TraceDecision>;
}): LayerOutcome[] {
  const policy = policyOutcome(input.gateway, input.live);
  return [
    policy,
    gateOutcome(input.checks, policy),
    approvalOutcome(input.lifecycles, input.decisions ?? new Map()),
    writeOutcome(input.writes, input.counts),
  ];
}

// --- the approval request (timeline approval_requested) -----------------------------------------------

export interface RequestFacts {
  expiresAt: string | null;
  /** How RECAP asked: "external (API/UI)", "console", "auto-approver (simulation only)". */
  channel: string | null;
}

/** expires_at and channel of each proposal's approval.requested event, by proposal id. */
export function requestFacts(entries: readonly TimelineEntry[] | null | undefined): Map<string, RequestFacts> {
  const facts = new Map<string, RequestFacts>();
  for (const entry of entries ?? []) {
    if (entry.kind !== "approval_requested" && entry.category !== "approval.requested") continue;
    const payload = isRecord(entry.payload) ? entry.payload : {};
    const id = text(payload.proposal_id);
    if (!id) continue;
    facts.set(id, { expiresAt: text(payload.expires_at) || null, channel: text(payload.channel) || null });
  }
  return facts;
}

// --- prompt-injection flags ------------------------------------------------------------------------

export interface InjectionRow {
  seq: number;
  evidenceId: string;
  tool: string | null;
  agent: string | null;
  patterns: string[];
  /** The flagged text as the tool returned it: untrusted data, never instructions. */
  excerpt: string | null;
}

/** The evidence view's flags, with each flag's excerpt from its trace entry (joined by seq). */
export function injectionRows(flags: readonly InjectionFlagView[] | null | undefined, entries: readonly TimelineEntry[] | null | undefined): InjectionRow[] {
  const bySeq = new Map<number, InjectionRow>();
  for (const flag of flags ?? []) {
    bySeq.set(flag.seq, {
      seq: flag.seq,
      evidenceId: text(flag.evidence_id),
      tool: text(flag.tool) || null,
      agent: text(flag.agent) || null,
      patterns: texts(flag.patterns),
      excerpt: null,
    });
  }
  for (const entry of entries ?? []) {
    if (entry.kind !== "injection_flag" && entry.category !== "safety.injection_flag") continue;
    const payload = isRecord(entry.payload) ? entry.payload : {};
    const known = bySeq.get(entry.seq);
    const excerpt = text(payload.excerpt) || null;
    if (known) known.excerpt = excerpt;
    else {
      bySeq.set(entry.seq, {
        seq: entry.seq,
        evidenceId: text(payload.evidence_id),
        tool: text(payload.tool) || text(entry.tool) || null,
        agent: text(entry.agent) || null,
        patterns: texts(payload.patterns),
        excerpt,
      });
    }
  }
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

// --- runs that show a safety stop (the Safety page's examples) ----------------------------------------

/** The outcomes (dto RunOutcome) that mean a safety layer or a person stopped the fix. */
export const SAFETY_STOP_OUTCOMES: Readonly<Record<string, string>> = {
  safety_blocked: "The policy or the evidence gate refused the fix",
  approval_rejected: "A person rejected the fix",
  approval_expired: "Nobody approved the fix in time",
};

export function isSafetyStop(outcome: unknown): boolean {
  return typeof outcome === "string" && Object.prototype.hasOwnProperty.call(SAFETY_STOP_OUTCOMES, outcome);
}

// --- proposals: the trusted risk tier and who wrote the rationale --------------------------------------

/**
 * The registry's risk tier for a proposal's action, as the policy recorded it when it judged the
 * fix (the controller's decision on that tool); null when no such decision was recorded. The
 * proposer's own declared risk is never trusted.
 */
export function trustedRisk(gateway: readonly GatewayRow[], action: string): string | null {
  const decision = gateway.find((row) => row.fix && row.tool === action && row.risk);
  return decision?.risk ?? null;
}

/** The policy's last decision on a proposed fix's tool (the controller's call), or null. */
export function lastFixDecision(gateway: readonly GatewayRow[], action: string): GatewayRow | null {
  for (let i = gateway.length - 1; i >= 0; i -= 1) {
    if (gateway[i].fix && gateway[i].tool === action) return gateway[i];
  }
  return null;
}

const AUTHORS: Readonly<Record<string, string>> = {
  model: "a model",
  replayed_model: "a model (replayed from a recording)",
  scripted_policy: "the scripted policy",
  recap: "RECAP",
};

/** Who wrote a text the API marks with an author (dto TextAuthor), in words. */
export function authorWords(author: string | null | undefined): string {
  const key = text(author);
  return Object.prototype.hasOwnProperty.call(AUTHORS, key) ? AUTHORS[key] : "an author the server did not record";
}

// --- what the test suite checks (documentation, shown as such) ------------------------------------------

export interface TestedGuarantee {
  file: string;
  test: string;
  /** What the test shows, in plain words. */
  shows: string;
  /**
   * M5: a guard for a target that is not simulated. These run in CI too, offline: on RECAP's
   * fake cluster or a target marked live, never on a real cluster.
   */
  liveTarget?: boolean;
}

/** Tests that pin these guarantees (they run in CI with `python -m pytest`). */
export const SAFETY_TESTS: readonly TestedGuarantee[] = [
  {
    file: "tests/adversarial/test_prompt_injection_gateway.py",
    test: "test_compromised_planner_is_blocked_by_the_gateway",
    shows: "A planner fooled by an injected log line proposes delete_namespace; the policy denies it, even after a tricked approval, and no write is sent.",
  },
  {
    file: "tests/adversarial/test_m4_safety.py",
    test: "test_a_fooled_fix_that_skips_the_schema_is_refused_by_the_evidence_gate",
    shows: "A fix aimed where the evidence does not point is refused by the evidence gate, although the policy alone would allow it after approval.",
  },
  {
    file: "tests/adversarial/test_audit_approval_race.py",
    test: "test_a_rejection_overrides_an_approval_written_by_another_process",
    shows: "A later no wins over an approval another process wrote first.",
  },
  {
    file: "tests/adversarial/test_audit_executor_authority.py",
    test: "test_a_used_approval_cannot_be_replayed_straight_to_the_server",
    shows: "A used approval cannot be replayed: sending the same write again from a second process changes nothing.",
  },
  {
    file: "tests/adversarial/test_m4_safety.py",
    test: "test_a_write_without_a_valid_capability_is_refused_and_changes_nothing",
    shows: "A write that reaches the environment directly is refused without the run's own signed capability for exactly that action.",
  },
  {
    file: "tests/adversarial/test_m4_safety.py",
    test: "test_a_correct_fix_still_runs_with_every_lure_present",
    shows: "Not a trivial refusal: with every lure present, a correct fix is still approved by a person, executed and verified by the oracle.",
  },
  {
    file: "tests/e2e/test_api_security.py",
    test: "test_a_live_approval_needs_the_target_services_exact_name",
    shows: "On a live target, an approval through the dashboard or the API needs the target service's exact name: one click, or a wrong name, decides nothing.",
    liveTarget: true,
  },
  {
    file: "tests/unit/test_console_approval_live.py",
    test: "test_anything_but_the_exact_name_rejects_on_a_live_target",
    shows: "At the terminal, y, yes, an Enter alone or any other text rejects a live write; the log says why, never what was typed.",
    liveTarget: true,
  },
  {
    file: "tests/unit/test_console_approval_live.py",
    test: "test_a_prompt_nobody_answers_gives_up_when_the_request_expires",
    shows: "A prompt nobody answers gives up when the request expires (an M5 run once waited four hours past it); a late line is ignored.",
    liveTarget: true,
  },
  {
    file: "tests/e2e/test_kind_pipeline.py",
    test: "test_the_write_boundary_holds_inside_the_mcp_server_for_any_caller",
    shows: "A caller that skips RECAP's gateway can read, but a forged capability, delete_namespace and a second use of a capability are refused inside the tool server.",
    liveTarget: true,
  },
  {
    file: "tests/unit/test_kind_engine.py",
    test: "test_delete_namespace_is_refused_even_with_a_valid_capability",
    shows: "The Kubernetes adapter performs only rollbacks and restarts: delete_namespace is refused even with a validly signed capability, before anything is sent.",
    liveTarget: true,
  },
  {
    file: "tests/e2e/test_live_target_provider_scope.py",
    test: "test_a_hosted_model_is_refused_before_a_live_target_is_touched",
    shows:
      "A free hosted or a paid model is refused before a live target is reset or read, so nothing of the target is observed (only the owner can permit a free hosted plan there).",
    liveTarget: true,
  },
];
