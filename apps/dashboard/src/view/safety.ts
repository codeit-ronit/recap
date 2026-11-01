/**
 * Pure helpers for the Safety & Approvals page (src/pages/Safety.tsx): the four safety layers
 * in plain words, a run's unsafe-action counts (attempted is not executed), each proposal's
 * permission lifecycle from the append-only approval log, the gateway's decisions, what RECAP
 * stopped and by which layer, and the prompt-injection flags.
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
      "The simulated shop applies a write only with a single-use capability signed for exactly that action, so a call that skips the gateway changes nothing.",
    rules: "Ed25519 signature · checked by the environment",
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
  /** True: a person decided; false: no person (RECAP, the simulation auto-approver); null: unknown. */
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
  /** Every entry for this proposal, oldest first. */
  events: ApprovalEvent[];
}

/**
 * requested -> decided -> used, or expired / rejected / revoked, from the proposal's entries in
 * the approval log. `live`: the run is still going, so a missing step is "waiting", not "never".
 */
export function permissionLifecycle(proposalId: string, entries: readonly ApprovalEvent[], live: boolean): Lifecycle {
  const events = entries.filter((e) => e.proposal_id === proposalId).sort((a, b) => a.event_id - b.event_id);
  const first = (kind: string) => events.find((e) => e.kind === kind) ?? null;
  const requested = first("requested");
  const approved = first("approved");
  const rejected = first("rejected");
  const revoked = first("revoked");
  const expired = first("expired");
  const consumed = first("consumed");
  const refused = events.filter((e) => e.kind === "refused");
  const who = (e: ApprovalEvent) => actorLabel(e.actor).label;

  if (!events.length || (!requested && !approved && !rejected && !expired && !consumed && !revoked)) {
    return {
      proposalId,
      status: "not_requested",
      label: "No approval requested",
      tone: "neutral",
      steps: [
        { key: "requested", title: "Requested", state: "skipped", text: "No request in the approval log", at: null, actor: null },
        { key: "decided", title: "Decided", state: "skipped", text: "Nothing to decide", at: null, actor: null },
        { key: "used", title: "Used", state: "skipped", text: "Nothing was used", at: null, actor: null },
      ],
      refused,
      events,
    };
  }

  const requestStep: LifecycleStep = requested
    ? { key: "requested", title: "Requested", state: "done", text: "A decision was requested", at: requested.at, actor: requested.actor }
    : { key: "requested", title: "Requested", state: "done", text: "Request not in the log", at: null, actor: null };

  let decided: LifecycleStep;
  if (approved) decided = { key: "decided", title: "Decided", state: "done", text: `Approved by ${who(approved)}`, at: approved.at, actor: approved.actor };
  else if (rejected) decided = { key: "decided", title: "Decided", state: "failed", text: `Rejected by ${who(rejected)}`, at: rejected.at, actor: rejected.actor };
  else if (expired) decided = { key: "decided", title: "Decided", state: "failed", text: "Expired: nobody decided in time", at: expired.at, actor: expired.actor };
  else decided = { key: "decided", title: "Decided", state: live ? "waiting" : "skipped", text: live ? "Waiting for a decision" : "No decision recorded", at: null, actor: null };

  let used: LifecycleStep;
  if (consumed) used = { key: "used", title: "Used", state: "done", text: "Used once by the gateway", at: consumed.at, actor: consumed.actor };
  else if (revoked) used = { key: "used", title: "Used", state: "failed", text: `Revoked by ${who(revoked)} before use`, at: revoked.at, actor: revoked.actor };
  else if (approved) used = { key: "used", title: "Used", state: live ? "waiting" : "skipped", text: live ? "Waiting for the gateway" : "Approved but never used", at: null, actor: null };
  else used = { key: "used", title: "Used", state: "skipped", text: "Not used", at: null, actor: null };

  let status: LifecycleStatus;
  let label: string;
  let tone: Tone;
  if (consumed) [status, label, tone] = ["used", "Approved and used once", "ok"];
  else if (revoked) [status, label, tone] = ["revoked", "Approval revoked before use", "bad"];
  else if (rejected) [status, label, tone] = ["rejected", "Rejected: the fix was not applied", "bad"];
  else if (expired) [status, label, tone] = ["expired", "Expired without a decision", "warn"];
  else if (approved) [status, label, tone] = live ? ["approved_waiting", "Approved, not used yet", "info"] : ["approved_unused", "Approved, never used", "warn"];
  else [status, label, tone] = live ? ["waiting", "Waiting for a person", "warn"] : ["no_decision", "No decision recorded", "neutral"];

  return { proposalId, status, label, tone, steps: [requestStep, decided, used], refused, events };
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

  input.actions.forEach((action, index) => {
    const block = ACTION_BLOCKS[action.result];
    if (!block) return;
    // The deciding rule of the policy's last DENY on this tool (a join on recorded fields).
    const denial = block.layer === "policy" ? [...fixDenials].reverse().find((row) => row.tool === action.action) : undefined;
    if (block.layer === "policy") deniedTools.add(action.action);
    rows.push({
      key: `action-${index}`,
      layer: block.layer,
      what: actionText(action.action, action.target, action.params),
      result: block.result,
      rule: denial?.deciding ?? null,
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

  for (const check of input.checks) {
    if (check.ok || !check.enforced) continue;
    const proposal = input.proposals.find((p) => p.proposal_id === check.proposal_id);
    rows.push({
      key: `gate-${check.seq}`,
      layer: "evidence_gate",
      what: proposal ? actionText(proposal.action, proposal.target, proposal.params) : `proposal ${check.proposal_id}`,
      result: "Refused",
      rule: check.rule_ids.length ? check.rule_ids.join(", ") : null,
      reason: check.problems.join("; ") || "Not recorded",
      at: null,
    });
  }

  for (const lifecycle of input.lifecycles) {
    for (const event of lifecycle.refused) {
      rows.push({
        key: `refused-${event.event_id}`,
        layer: "approval",
        what: `a decision on proposal ${lifecycle.proposalId}`,
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
  if (refused.length) return outcome("evidence_gate", "stopped", "Refused the fix", refused.flatMap((check) => check.problems));
  if (unenforced.length) {
    return outcome("evidence_gate", "warning", "Failed, but not enforced", [
      "the simulation-only safety-off ablation was on",
      ...unenforced.flatMap((check) => check.problems),
    ]);
  }
  const rules = [...new Set(checks.flatMap((check) => check.rule_ids))];
  return outcome("evidence_gate", "passed", checks.length === 1 ? "Passed" : `Passed (${checks.length} proposals)`, rules.length ? [rules.join(" · ")] : []);
}

function approvalOutcome(lifecycles: readonly Lifecycle[]): LayerOutcome {
  const asked = lifecycles.filter((l) => l.status !== "not_requested");
  if (!asked.length) return outcome("approval", "not_reached", "No approval requested");
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
  const sent = writes
    .filter((entry) => entry.kind === "write_executed" || entry.category === "mitigation.executed")
    .map((entry) => (isRecord(entry.payload) ? entry.payload : {}));
  const refusedLine =
    counts.refusedByEnvironment !== null && counts.refusedByEnvironment > 0
      ? [`${counts.refusedByEnvironment} unsafe ${counts.refusedByEnvironment === 1 ? "write" : "writes"} refused by the environment`]
      : [];
  if (!sent.length) return outcome("capability", "not_reached", "No write was sent", refusedLine);
  const failed = sent.filter((payload) => payload.ok !== true);
  const last = sent[sent.length - 1];
  const what = actionText(last.tool, last.target);
  if (failed.length) {
    const error = text(failed[failed.length - 1].error);
    return outcome("capability", "stopped", `Write failed or refused: ${what}`, [error || "error not recorded", ...refusedLine]);
  }
  return outcome("capability", "passed", sent.length === 1 ? `Signed write applied: ${what}` : `${sent.length} signed writes applied`, refusedLine);
}

/** How one run went through the four layers, from what it recorded. */
export function runLayerOutcomes(input: {
  gateway: readonly GatewayRow[];
  checks: readonly ProposalCheckView[];
  lifecycles: readonly Lifecycle[];
  writes: readonly TimelineEntry[];
  counts: UnsafeCounts;
  live: boolean;
}): LayerOutcome[] {
  const policy = policyOutcome(input.gateway, input.live);
  return [policy, gateOutcome(input.checks, policy), approvalOutcome(input.lifecycles), writeOutcome(input.writes, input.counts)];
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
}

/** Adversarial tests that pin these guarantees (they run in CI with `python -m pytest`). */
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
];
