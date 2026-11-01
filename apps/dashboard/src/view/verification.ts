/**
 * View helpers for the verification panels (Evidence & Decisions, Live Run, Trace Explorer): who
 * wrote the agents' text, how each checked claim used its evidence, the disagreements in plain
 * words, the verifier's leads, and the merge history read from verification.merge events. The
 * M4 section at the end reads GET /runs/{id}/evidence (the typed EvidenceView).
 *
 * Pure functions with type-only imports, so the Node tests in ../../tests load this file
 * directly. Runs recorded before M3 lack proposers, basis and disagreements. Read back through
 * the API they arrive as empty lists (pydantic defaults), so an empty list is never taken as a
 * recorded fact: an M3 check always has a proposer, so a report whose checks have none was
 * verified before M3, and those values come back as null ("not recorded").
 */

import type {
  CheckView,
  ClaimView,
  EvidenceItem,
  EvidenceView,
  FindingView,
  PolicyVerdictView,
  ProposalCheckView,
  TextAuthor as AuthorKind,
  VerificationView,
} from "../dto";
import type { Tone } from "../format";
import type { AgentFinding, Evidence, Hypothesis, HypothesisCheck, ProviderInfo, TraceEvent, VerificationReport } from "../types";

// --- defensive readers (local copies: this module imports no runtime values) ------------------

function arr(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function strs(value: unknown): string[] {
  return arr(value).filter((v): v is string => typeof v === "string");
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function unique<T>(items: Iterable<T>): T[] {
  return [...new Set(items)];
}

// --- who wrote the agents' text ------------------------------------------------------------------

export interface TextAuthor {
  /** For tags: "model", "scripted policy", "replayed model", "agent". */
  who: string;
  /** For a confidence note: "the model's own words", ... */
  words: string;
}

/**
 * Who wrote the agents' text in a run (claims, confidence notes, search queries). That text is
 * untrusted whoever wrote it, and a scripted or replayed run is never labeled live reasoning.
 */
export function textAuthor(provider: Partial<ProviderInfo> | null | undefined): TextAuthor {
  switch (provider?.origin ?? provider?.kind) {
    case "scripted":
      return { who: "scripted policy", words: "the scripted policy's words" };
    case "replay":
      return { who: "replayed model", words: "a recorded model's own words (replayed)" };
    case "local_llm":
    case "free_hosted_llm":
    case "paid_llm":
    case "llm":
      return { who: "model", words: "the model's own words" };
    default:
      return { who: "agent", words: "the agent's own words" };
  }
}

// --- runs verified before M3 ----------------------------------------------------------------------

/**
 * True when the record predates M3's verification records: every check lacks proposers (an M3
 * check always has one, merger.merge_claims), or every finding lacks an outcome (M3 always
 * sets one). The API serves the missing lists as [], so only these markers tell.
 */
export function verifiedBeforeM3(
  report: Pick<VerificationReport, "checks"> | null | undefined,
  findings?: readonly Pick<AgentFinding, "outcome">[] | null,
): boolean {
  const checks = report?.checks ?? [];
  if (checks.length && checks.every((c) => !c.proposers?.length)) return true;
  return !!findings?.length && findings.every((f) => !f.outcome);
}

// --- how a checked claim used its evidence ------------------------------------------------------

/**
 * basis: a passing rule relied on it · cited: cited, but no passing rule relied on it ·
 * cited_unrecorded: cited, and the run did not record what the rules relied on (before M3) ·
 * never_proof: not a tool result (a search receipt, say), which the rules never read ·
 * against: evidence against the claim · unknown: not in this run's evidence registry.
 */
export type CiteKind = "basis" | "cited" | "cited_unrecorded" | "never_proof" | "against" | "unknown";

export interface EvidenceRef {
  id: string;
  kind: CiteKind;
  /** The tool that produced it, and the agent whose call recorded it (null: the controller). */
  tool: string | null;
  recordedBy: string | null;
  /** False when no evidence with this id is in the run's registry. */
  found: boolean;
}

export interface EvidenceUse {
  /** What the claim cites as support, in its own order. */
  cited: EvidenceRef[];
  /** What the passing rules relied on (M3), in the verifier's order; null: not recorded. */
  basis: (EvidenceRef & { citedByClaim: boolean })[] | null;
  contradicting: EvidenceRef[];
}

export function evidenceUse(check: HypothesisCheck, byId: ReadonlyMap<string, Evidence>): EvidenceUse {
  const h = check.hypothesis;
  // Checked before M3 (no proposers): the API's empty basis list is a default, not a record.
  const basis = check.proposers?.length ? (check.basis_evidence_ids ?? []) : null;
  const inBasis = new Set(basis ?? []);
  const cites = new Set(h.supporting_evidence_ids);
  const kindOf = (id: string, e: Evidence | undefined, against: boolean): CiteKind => {
    if (against) return e ? "against" : "unknown";
    if (inBasis.has(id)) return "basis";
    if (!e) return "unknown";
    if (e.source_type !== "tool") return "never_proof";
    return basis ? "cited" : "cited_unrecorded";
  };
  const ref = (id: string, against = false): EvidenceRef => {
    const e = byId.get(id);
    return { id, kind: kindOf(id, e, against), tool: e?.tool_name ?? null, recordedBy: e?.agent_role ?? null, found: !!e };
  };
  return {
    cited: unique(h.supporting_evidence_ids).map((id) => ref(id)),
    basis: basis ? unique(basis).map((id) => ({ ...ref(id), citedByClaim: cites.has(id) })) : null,
    contradicting: unique(h.contradicting_evidence_ids).map((id) => ref(id, true)),
  };
}

const KIND_NOTES: Record<CiteKind, string> = {
  basis: "a passing rule relied on it",
  cited: "cited, but no passing rule relied on it",
  cited_unrecorded: "cited (what the rules relied on was not recorded for this run)",
  never_proof: "not a tool result: the evidence rules never read it, so it proves nothing",
  against: "evidence against the claim",
  unknown: "not in this run's evidence registry",
};

/** A tooltip for one evidence id: what it is, who recorded it, how the verifier used it. */
export function evidenceTitle(ref: EvidenceRef): string {
  if (!ref.found) {
    const note = ref.kind === "unknown" ? "" : ` · ${KIND_NOTES[ref.kind]}`;
    return `${ref.id} · ${KIND_NOTES.unknown}${note}`;
  }
  const who = ref.recordedBy ? `recorded by ${ref.recordedBy}` : "recorded by the controller";
  return `${ref.id} · ${ref.tool ?? "no tool"} · ${who} · ${KIND_NOTES[ref.kind]}`;
}

/** Every agent that made the claim (M3); before M3 only the first proposer was recorded. */
export function proposersOf(check: HypothesisCheck): { agents: string[]; recorded: boolean } {
  if (check.proposers?.length) return { agents: unique(check.proposers), recorded: true };
  const first = check.hypothesis.proposed_by;
  return { agents: first ? [first] : [], recorded: false };
}

/** The details a claim names that the rules check: a release, a blamed dependency, an episode. */
export function claimDetails(details: Record<string, unknown> | null | undefined): { label: string; value: string }[] {
  const d = record(details);
  const out: { label: string; value: string }[] = [];
  if (text(d.version)) out.push({ label: "release", value: text(d.version) });
  if (text(d.dependency)) out.push({ label: "blames", value: text(d.dependency) });
  if (text(d.episode_id)) out.push({ label: "episode", value: text(d.episode_id) });
  return out;
}

function cause(mechanism: string, target: string | null | undefined): string {
  return `${mechanism || "?"}@${target || "?"}`;
}

/** "bad_deployment@payment": how this page names a claim's cause. */
export function causeLabel(h: Pick<Hypothesis, "mechanism" | "target_service">): string {
  return cause(h.mechanism, h.target_service);
}

// Besides origin, mechanism and target, the merger keys a claim by this detail (recap/agents/merger.py).
const DETAIL_KEYS: Record<string, string> = { bad_deployment: "version", dependency_failure: "dependency" };

/** The detail a hypothesis names itself (its release or blamed dependency); "" when none. */
export function namedDetail(h: Pick<Hypothesis, "mechanism" | "details"> | null | undefined): string {
  const key = DETAIL_KEYS[text(h?.mechanism)];
  const value = key ? record(h?.details)[key] : undefined;
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

/** The detail the merger keyed a check by (HypothesisCheck.detail, M3), else the one its claim names. */
export function checkDetail(check: Pick<HypothesisCheck, "hypothesis" | "detail">): string {
  return text(check.detail) || namedDetail(check.hypothesis);
}

// Only values of these shapes are known not to be free text: a release version (with or
// without its "v", as the verifier reads it) and a service name (a DNS label).
const VERSION_SHAPE = /^[vV]?[0-9]+\.[0-9]+\.[0-9]+$/;
const SERVICE_SHAPE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

/**
 * True when a detail has the shape of a checked value (a version, a service name). Anything
 * else came from the claim as written, so a model may have put any text there: show it as data.
 */
export function isCheckedValue(mechanism: string, value: string): boolean {
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
 * Per failed rule line of a check: true when the line repeats a release or dependency the
 * claim named that is not shaped like one. The line is RECAP's sentence, but that part is the
 * agent's own text (a rule line quotes the claim's version as written), so the page tags it.
 */
export function failedQuotesClaim(check: Pick<HypothesisCheck, "hypothesis" | "failed" | "detail">): boolean[] {
  const mechanism = check.hypothesis.mechanism;
  const values = unique([namedDetail(check.hypothesis).trim(), text(check.detail).trim()]).filter((v) => v && !isCheckedValue(mechanism, v));
  return arr(check.failed).map((line) => typeof line === "string" && values.some((value) => repeats(line, value)));
}

/**
 * A detail a claim names, by its key: a release ("version") or a blamed dependency
 * ("dependency") is a checked value only in its shape; other details (a remembered claim's
 * episode) are RECAP's own.
 */
export function isCheckedDetail(key: string, value: string): boolean {
  if (key === "version") return VERSION_SHAPE.test(value);
  if (key === "dependency") return SERVICE_SHAPE.test(value);
  return true;
}

/** "release v2.4.1" or "blames payment": a check's detail in words; "" when it has none. */
export function detailLabel(mechanism: string, detail: string): string {
  if (!detail) return "";
  return mechanism === "dependency_failure" ? `blames ${detail}` : mechanism === "bad_deployment" ? `release ${detail}` : detail;
}

function sameClaim(a: Hypothesis, b: Hypothesis): boolean {
  return (
    a.claim === b.claim &&
    a.mechanism === b.mechanism &&
    a.target_service === b.target_service &&
    a.origin === b.origin &&
    namedDetail(a) === namedDetail(b)
  );
}

/**
 * The check whose hypothesis became the diagnosis (report.best): always a supported check, and
 * the same release or dependency too, since two claims worded alike can name different ones.
 */
export function isDiagnosis(report: VerificationReport | null | undefined, check: HypothesisCheck): boolean {
  return !!report?.best && check.hypothesis.status === "supported" && sameClaim(report.best, check.hypothesis);
}

/** The diagnosis' own check (report.best), for the detail the merger keyed it by. */
export function diagnosisCheck(report: VerificationReport | null | undefined): HypothesisCheck | null {
  return report?.checks.find((c) => isDiagnosis(report, c)) ?? null;
}

export interface EvidenceRole {
  /** The claims (cause labels) whose passing rules relied on this item. */
  basisOf: string[];
  /** The diagnosis relied on it. */
  diagnosis: boolean;
  /** The claims it contradicts. */
  contradicts: string[];
}

/**
 * For the evidence registry: what the final verification did with each evidence item. A claim
 * is named with its detail ("bad_deployment@checkout (v2.4.0)"), so two releases stay apart.
 */
export function evidenceRoles(report: VerificationReport | null | undefined): Map<string, EvidenceRole> {
  const roles = new Map<string, EvidenceRole>();
  const roleOf = (id: string): EvidenceRole => {
    let role = roles.get(id);
    if (!role) roles.set(id, (role = { basisOf: [], diagnosis: false, contradicts: [] }));
    return role;
  };
  for (const check of report?.checks ?? []) {
    const detail = checkDetail(check);
    const label = `${causeLabel(check.hypothesis)}${detail ? ` (${detail})` : ""}`;
    const diagnosis = isDiagnosis(report, check);
    for (const id of check.basis_evidence_ids ?? []) {
      const role = roleOf(id);
      if (!role.basisOf.includes(label)) role.basisOf.push(label);
      if (diagnosis) role.diagnosis = true;
    }
    for (const id of check.hypothesis.contradicting_evidence_ids) {
      const role = roleOf(id);
      if (!role.contradicts.includes(label)) role.contradicts.push(label);
    }
  }
  return roles;
}

// --- verifier rule ids (documentation for tooltips, from recap/agents/verifier.py) --------------

export const VERIFIER_RULES: Record<string, string> = {
  "V1-structural": "a target, a known mechanism, and cited evidence that exists in this run",
  "V1-grounding": "a supported claim cites at least one item its rules relied on",
  "V2-deploy-timing": "bad deployment: the service's errors began 5 min before to 30 min after its deploy",
  "V2-upstream-dominance": "bad deployment: fewer than half of the service's ERROR lines blame a dependency not observed healthy",
  "V3-resource": "resource pressure: OOM kills or memory at 90% of its limit or more; OOM log lines only corroborate",
  "V4-dependency": "dependency failure: the service's logs show calls to the dependency failing; never actionable by itself",
  "V5-memory": "a remembered claim faces the same rules (no free pass)",
  "V6-conflict-resolution": "when log text disagrees with platform signals (health, metrics, cluster events), the platform signals win",
  "V6-healthy-target": "an actionable claim on a service the platform shows healthy is contradicted: there is nothing on it to fix",
  "V7-deepest-cause": "of the supported actionable claims, the one the most services' upstream errors lead to wins; level root causes stay open",
};

export function ruleNote(id: string): string | undefined {
  return VERIFIER_RULES[id];
}

/** A disagreement's rule_id may list several rules ("V2-upstream-dominance, V6-healthy-target"). */
export function ruleIdsOf(ruleId: string | null | undefined): string[] {
  return unique(
    (ruleId ?? "")
      .split(",")
      .map((id) => id.trim())
      .filter(Boolean),
  );
}

// --- disagreements -------------------------------------------------------------------------------

/** A disagreement as this page shows it, from the run record or from a trace payload. */
export interface DisagreementView {
  kind: string;
  claims: string[];
  agents: string[];
  evidence_ids: string[];
  resolved: boolean;
  rule_id: string;
  resolution: string;
  question: string;
}

/** Each kind in plain words (recap/agents/merger.py). */
export const DISAGREEMENT_KINDS: Record<string, { label: string; plain: string }> = {
  detail_conflict: {
    label: "Detail conflict",
    plain: "two versions of one release: claims blame different releases of the same service",
  },
  competing_root_causes: {
    label: "Competing root causes",
    plain: "two different root causes are both supported by the evidence",
  },
  cross_agent_contradiction: {
    label: "Cross-agent contradiction",
    plain: "one agent's claim contradicted by another agent's evidence",
  },
  missing_view: {
    label: "Missing view",
    plain: "an agent ended without a usable finding, so its view is missing",
  },
};

export function disagreementKind(kind: string): { label: string; plain: string } {
  return DISAGREEMENT_KINDS[kind] ?? { label: kind ? kind.replace(/_/g, " ") : "unknown kind", plain: "" };
}

/** OPEN: no evidence rule settled it (the next round is asked, then a person). */
export function disagreementBadge(resolved: boolean): { label: "OPEN" | "SETTLED"; tone: Tone } {
  return resolved ? { label: "SETTLED", tone: "ok" } : { label: "OPEN", tone: "warn" };
}

/** A disagreement record read defensively (trace payloads are untyped). */
export function readDisagreement(raw: unknown): DisagreementView {
  const d = record(raw);
  return {
    kind: text(d.kind),
    claims: strs(d.claims),
    agents: strs(d.agents),
    evidence_ids: strs(d.evidence_ids),
    resolved: d.resolved === true,
    rule_id: text(d.rule_id),
    resolution: text(d.resolution),
    question: text(d.question),
  };
}

/**
 * The final verification's disagreements; null when the run did not record them (before M3:
 * see verifiedBeforeM3, as the API serves the missing list as []).
 */
export function finalDisagreements(
  report: VerificationReport | null | undefined,
  findings?: readonly Pick<AgentFinding, "outcome">[] | null,
): DisagreementView[] | null {
  if (!report?.disagreements || verifiedBeforeM3(report, findings)) return null;
  return report.disagreements.map(readDisagreement);
}

/** The verifier's leads for the next round (RECAP's own text, M3); [] when it wrote none or predates them. */
export function reportLeads(report: Pick<VerificationReport, "leads"> | null | undefined): string[] {
  return unique(strs(report?.leads));
}

export interface AskedRound {
  round: number | null;
  /** The agents whose agent.start carried the question. */
  agents: string[];
}

/** The rounds that were asked an open question (investigation.round) and the agents given it (agent.start). */
export function roundsAsked(question: string, events: readonly TraceEvent[]): AskedRound[] {
  const asked: AskedRound[] = [];
  if (!question) return asked;
  const entry = (round: number | null): AskedRound => {
    let found = asked.find((a) => a.round === round);
    if (!found) asked.push((found = { round, agents: [] }));
    return found;
  };
  for (const e of events) {
    if (e.category === "investigation.round" && strs(e.payload.open_questions).includes(question)) {
      entry(count(e.payload.round));
    } else if (e.category === "agent.start" && strs(e.payload.questions).includes(question)) {
      const agent = text(e.payload.role) || e.agent || "";
      const round = entry(count(e.payload.round));
      if (agent && !round.agents.includes(agent)) round.agents.push(agent);
    }
  }
  return asked.sort((a, b) => (a.round ?? 0) - (b.round ?? 0));
}

function roundsText(rounds: readonly AskedRound[]): string {
  const nums = rounds.map((r) => (r.round === null ? "?" : String(r.round)));
  const list = nums.length > 1 ? `rounds ${nums.slice(0, -1).join(", ")} and ${nums[nums.length - 1]}` : `round ${nums[0]}`;
  const agents = unique(rounds.flatMap((r) => r.agents));
  return agents.length ? `${list} (${agents.join(", ")})` : list;
}

/**
 * What became of an open question, for the verification that closed `round`: the same open
 * conflict may already have had its follow-up round (rounds up to `round`), and a later round
 * may have been asked it. Only what the trace shows.
 */
export function askedSummary(asked: readonly AskedRound[], round: number | null): string {
  const earlier = round === null ? [] : asked.filter((a) => a.round !== null && a.round <= round);
  const later = asked.filter((a) => !earlier.includes(a));
  const lines: string[] = [];
  if (earlier.length) lines.push(`Already asked in ${roundsText(earlier)}; the conflict stayed open.`);
  lines.push(later.length ? `Asked in ${roundsText(later)}.` : "No later round was asked this question.");
  return lines.join(" ");
}

// --- merge history (verification.merge) ---------------------------------------------------------

export interface SharedClaim {
  /** mechanism@target */
  cause: string;
  proposers: string[];
  /** Merged claims with this cause and these proposers (a live and a remembered one, say). */
  count: number;
}

export interface MergeRow {
  seq: number;
  /** The investigation round this verification closed (the last investigation.round before it). */
  round: number | null;
  /** Claims each agent made this epoch so far (every round since the last fix). */
  claimsByAgent: { agent: string; claims: number | null }[];
  mergedClaims: number | null;
  shared: SharedClaim[];
  /** Full records from the verification.evidence event that followed, else the short form. */
  disagreements: DisagreementView[];
  openQuestions: string[];
  /** The verifier's leads for the next round (RECAP's own text); [] when none or not traced. */
  leads: string[];
  /** That verification's verdict, and the cause it diagnosed (null: none or not traced). */
  status: string | null;
  best: string | null;
}

/** One row per verification, from verification.merge and the verification.evidence after it. */
export function mergeHistory(events: readonly TraceEvent[]): MergeRow[] {
  const sorted = [...events].sort((a, b) => a.seq - b.seq);
  const rows: MergeRow[] = [];
  let round: number | null = null;
  for (let i = 0; i < sorted.length; i++) {
    const e = sorted[i];
    if (e.category === "investigation.round") round = count(e.payload.round) ?? round;
    if (e.category !== "verification.merge") continue;
    const p = e.payload;
    const verdict = verdictAfter(sorted, i) ?? {};
    const best = record(verdict.best);
    // Both lists come from the same report; the full one also has the resolution and question.
    const full = verdict.disagreements;
    rows.push({
      seq: e.seq,
      round,
      claimsByAgent: Object.entries(record(p.claims_by_agent)).map(([agent, n]) => ({ agent, claims: count(n) })),
      mergedClaims: count(p.merged_claims),
      shared: sharedClaims(p.shared_claims),
      disagreements: (Array.isArray(full) ? full : arr(p.disagreements)).map(readDisagreement),
      openQuestions: strs(p.open_questions),
      leads: unique([...strs(p.leads), ...strs(verdict.leads)]),
      status: text(verdict.status) || null,
      best: text(best.mechanism) ? cause(text(best.mechanism), text(best.target_service)) : null,
    });
  }
  return rows;
}

/** The report (not a proposal check) traced right after merge event i, before the next merge. */
function verdictAfter(events: readonly TraceEvent[], i: number): Record<string, unknown> | null {
  for (const e of events.slice(i + 1)) {
    if (e.category === "verification.merge") return null;
    if (e.category === "verification.evidence" && "status" in e.payload) return e.payload;
  }
  return null;
}

function sharedClaims(raw: unknown): SharedClaim[] {
  const out: SharedClaim[] = [];
  for (const item of arr(raw)) {
    const c = record(item);
    const label = cause(text(c.mechanism), text(c.target));
    const proposers = strs(c.proposers);
    const same = out.find((s) => s.cause === label && s.proposers.join(",") === proposers.join(","));
    if (same) same.count += 1;
    else out.push({ cause: label, proposers, count: 1 });
  }
  return out;
}

// =================================================================================================
// M4: GET /runs/{id}/evidence (EvidenceView, generated in ../dto). Pure helpers for the Evidence &
// Decisions page: who wrote each text, the claims per round and agent with the check that judged
// them, the supported diagnosis apart from the claims that were not accepted, disagreements, the
// evidence registry's kinds, trust classes and uses, and evidence ids inside RECAP's rule lines.
// =================================================================================================

/** What a field the server did not record reads as. */
export const NOT_RECORDED = "Not recorded";

/** A dto TextAuthor (who wrote a claim, a summary, a rationale or a query) in words. */
export function authorFromKind(kind: AuthorKind | null | undefined): TextAuthor {
  switch (kind) {
    case "scripted_policy":
      return { who: "scripted policy", words: "the scripted policy's words" };
    case "replayed_model":
      return { who: "replayed model", words: "a recorded model's own words (replayed)" };
    case "model":
      return { who: "model", words: "the model's own words" };
    case "recap":
      return { who: "RECAP", words: "RECAP's own text" };
    default:
      return { who: "agent", words: "the agent's own words" };
  }
}

/** "untrusted · written by the replayed model": the tag on a text a provider wrote. */
export function untrustedTag(kind: AuthorKind | null | undefined): string {
  const who = authorFromKind(kind).who;
  return kind === "recap" ? "RECAP's text" : `untrusted · written by the ${who}`;
}

// --- evidence kinds and trust classes ----------------------------------------------------------

export type EvidenceKindKey =
  | "logs"
  | "metrics"
  | "deployments"
  | "events"
  | "health"
  | "services"
  | "action"
  | "runbook"
  | "memory"
  | "alert"
  | "oracle"
  | "other";

export const EVIDENCE_KIND_LABELS: Readonly<Record<EvidenceKindKey, string>> = {
  logs: "Logs",
  metrics: "Metrics",
  deployments: "Deployments",
  events: "Cluster events",
  health: "Health",
  services: "Service list",
  action: "Action result",
  runbook: "Runbook search",
  memory: "Memory",
  alert: "Alert",
  oracle: "Oracle",
  other: "Other",
};

// The simulated shop's MCP tools (recap/tools/mcp_server.py) and RECAP's own search_runbooks.
const TOOL_KINDS: Readonly<Record<string, EvidenceKindKey>> = {
  get_service_logs: "logs",
  get_service_metrics: "metrics",
  get_recent_deployments: "deployments",
  propose_rollback: "deployments",
  get_k8s_like_events: "events",
  get_service_health: "health",
  list_services: "services",
  execute_rollback: "action",
  restart_service: "action",
  delete_namespace: "action",
  search_runbooks: "runbook",
};

/** What an observation is (logs, metrics, deployments, ...), from its source and tool only. */
export function evidenceKind(item: { source_type: string; tool: string | null }): EvidenceKindKey {
  if (item.source_type === "tool" || item.source_type === "doc") {
    const kind = item.tool ? TOOL_KINDS[item.tool] : undefined;
    if (kind) return kind;
    return item.source_type === "doc" ? "runbook" : "other";
  }
  if (item.source_type === "memory" || item.source_type === "alert" || item.source_type === "oracle") return item.source_type;
  return "other";
}

/** A trust class (EvidenceItem.trust_class) in two or three words. */
export function trustClassLabel(trustClass: string): string {
  switch (trustClass) {
    case "untrusted_tool_output":
      return "tool output";
    case "untrusted_runbook_text":
      return "runbook text";
    case "untrusted_memory_text":
      return "memory text";
    case "untrusted_alert_text":
      return "alert text";
    case "recap_trusted_text":
      return "RECAP (trusted)";
    default:
      return trustClass ? trustClass.replace(/_/g, " ") : NOT_RECORDED;
  }
}

/** True for every class but RECAP's own text. */
export function isUntrustedClass(trustClass: string): boolean {
  return trustClass !== "recap_trusted_text";
}

// --- matching a claim to the check that judged it ------------------------------------------------

/** The remembered-claim rule: the verifier adds it to every check of a claim from memory (and only those). */
export const MEMORY_RULE = "V5-memory";

/** The release (bad deployment) or dependency (dependency failure) a claim names itself; "" when none. */
export function claimDetailOf(claim: { mechanism: string; details: Record<string, unknown> | null | undefined }): string {
  const key = DETAIL_KEYS[claim.mechanism];
  const value = key ? record(claim.details)[key] : undefined;
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
}

/** A version without its case and one leading "v", as the verifier compares releases. */
function looseVersion(value: string): string {
  return value.trim().toLowerCase().replace(/^v/, "");
}

function sameDetail(mechanism: string, a: string, b: string): boolean {
  return mechanism === "bad_deployment" ? looseVersion(a) === looseVersion(b) : a.trim() === b.trim();
}

/**
 * The check (of one verification) that judged a claim. The merger keys a claim by its origin,
 * mechanism, target and detail, so the check must have the same mechanism and target, the
 * claim's agent among its proposers, the same origin (a remembered claim's check notes
 * V5-memory) and the release or dependency the claim names. The claim's own text breaks a tie
 * (the merged check keeps its first proposer's words). null: no single check matches.
 */
export function checkOfClaim(
  claim: Pick<ClaimView, "agent" | "claim" | "mechanism" | "target_service" | "details" | "origin">,
  checks: readonly CheckView[] | null | undefined,
): CheckView | null {
  if (!checks?.length) return null;
  const withProposers = checks.some((c) => c.proposers.length > 0);
  let found = checks.filter(
    (c) =>
      c.mechanism === claim.mechanism &&
      (c.target ?? "") === (claim.target_service ?? "") &&
      (!withProposers || c.proposers.includes(claim.agent)),
  );
  if (found.some((c) => c.rule_ids.length)) {
    const remembered = claim.origin === "memory";
    found = found.filter((c) => c.rule_ids.includes(MEMORY_RULE) === remembered);
  }
  const named = claimDetailOf(claim);
  if (named && found.some((c) => c.detail)) found = found.filter((c) => sameDetail(claim.mechanism, c.detail, named));
  if (found.length > 1) {
    const sameText = found.filter((c) => c.claim === claim.claim);
    if (sameText.length) found = sameText;
  }
  return found.length === 1 ? found[0] : null;
}

/**
 * The check behind the report's diagnosis (report.best): a supported check with the same text,
 * mechanism and target. When a live and a remembered claim tie, the verifier ranks the live one
 * first (V7), so the check without V5-memory is preferred. null: none, or no single one.
 */
export function diagnosisCheckOf(report: Pick<VerificationView, "best" | "checks"> | null | undefined): CheckView | null {
  const best = report?.best;
  if (!best) return null;
  let found = report.checks.filter(
    (c) => c.status === "supported" && c.claim === best.claim && c.mechanism === best.mechanism && (c.target ?? "") === (best.target_service ?? ""),
  );
  if (found.length > 1) {
    const live = found.filter((c) => !c.rule_ids.includes(MEMORY_RULE));
    if (live.length) found = live;
  }
  return found.length === 1 ? found[0] : null;
}

/** "resource pressure on inventory": a cause in words (mechanism, target and its detail). */
export function causeWords(mechanism: string, target: string | null | undefined, detail = ""): string {
  const words = mechanism ? mechanism.replace(/_/g, " ") : "unknown mechanism";
  const on = `${words} on ${target || "an unnamed service"}`;
  if (!detail) return on;
  return mechanism === "dependency_failure" ? `${on} (blames ${detail})` : mechanism === "bad_deployment" ? `${on} (release ${detail})` : on;
}

export interface ReportSplit {
  /** The check behind the diagnosis (report.best), when one matches. */
  diagnosis: CheckView | null;
  /** Supported, but not the diagnosis (a dependency failure only points elsewhere, say). */
  alsoSupported: CheckView[];
  /** Insufficient, contradicted, or any other status: not accepted. */
  notAccepted: CheckView[];
  counts: { supported: number; insufficient: number; contradicted: number; other: number };
}

/** The final report's checks: the supported diagnosis apart from the claims not accepted. */
export function splitReport(report: Pick<VerificationView, "best" | "checks"> | null | undefined): ReportSplit {
  const diagnosis = diagnosisCheckOf(report);
  const checks = report?.checks ?? [];
  const counts = { supported: 0, insufficient: 0, contradicted: 0, other: 0 };
  for (const c of checks) {
    if (c.status === "supported" || c.status === "insufficient" || c.status === "contradicted") counts[c.status] += 1;
    else counts.other += 1;
  }
  return {
    diagnosis,
    alsoSupported: checks.filter((c) => c.status === "supported" && c !== diagnosis),
    notAccepted: checks.filter((c) => c.status !== "supported"),
    counts,
  };
}

// --- the causal chain: claims per round and agent ---------------------------------------------

export interface ChainClaim {
  claim: ClaimView;
  /** The check that judged the claim after its round (or the final report's, when untraced). */
  check: CheckView | null;
  /** "round": the verification that closed the claim's round · "final": the run record's report. */
  checkSource: "round" | "final" | null;
  /** The final verification's verdict on the same claim, when it differs from `check`'s. */
  finalStatus: string | null;
  /** The claim the final report diagnosed (several agents can make the same claim). */
  diagnosis: boolean;
}

export interface ChainAgent {
  finding: FindingView;
  claims: ChainClaim[];
}

export interface ChainRound {
  /** null: verifications the trace did not place in a round. */
  round: number | null;
  agents: ChainAgent[];
  /** The last traced verification of this round (the final report when the run has no trace). */
  verification: VerificationView | null;
}

/** The last traced verification of a round, or null. */
export function verificationOfRound(verifications: readonly VerificationView[], round: number | null): VerificationView | null {
  const found = verifications.filter((v) => v.round === round);
  return found.length ? found[found.length - 1] : null;
}

/**
 * Every round with its agents' claims, each with the check that judged it (rules, never votes)
 * and the verification that closed the round. Claims keep the order the run recorded them.
 */
export function causalChain(view: Pick<EvidenceView, "findings" | "verifications" | "final_report">): ChainRound[] {
  const final = view.final_report;
  const diagnosis = diagnosisCheckOf(final);
  const traced = view.verifications.length > 0;
  const rounds = unique<number | null>([...view.findings.map((f) => f.round), ...view.verifications.map((v) => v.round)]);
  if (!traced && final && !rounds.includes(final.round)) rounds.push(final.round);
  rounds.sort((a, b) => (a === null ? 1 : b === null ? -1 : a - b));
  return rounds.map((round) => {
    const verification = traced ? verificationOfRound(view.verifications, round) : final && final.round === round ? final : null;
    const agents = view.findings
      .filter((f) => f.round === round)
      .map((finding) => ({
        finding,
        claims: finding.claims.map((claim) => {
          const own = traced ? checkOfClaim(claim, verification?.checks) : null;
          const last = checkOfClaim(claim, final?.checks);
          const check = own ?? (traced ? null : last);
          const checkSource: ChainClaim["checkSource"] = own ? "round" : check ? "final" : null;
          return {
            claim,
            check,
            checkSource,
            finalStatus: own && last && last.status !== own.status ? last.status : null,
            diagnosis: !!last && last === diagnosis,
          };
        }),
      }));
    return { round, agents, verification };
  });
}

// --- disagreements across verifications ------------------------------------------------------------

export interface DisagreementEntry {
  view: DisagreementView;
  /** The rounds whose verification recorded it (the same record can recur round after round). */
  rounds: (number | null)[];
  /** Recorded by the final verification too. */
  final: boolean;
}

/** Every disagreement the verifications recorded, each once, with the rounds that recorded it. */
export function disagreementsOf(view: Pick<EvidenceView, "verifications" | "final_report">): DisagreementEntry[] {
  const entries = new Map<string, DisagreementEntry>();
  const sources = [...view.verifications, ...(view.final_report ? [view.final_report] : [])];
  for (const v of sources) {
    for (const raw of v.disagreements) {
      const d = readDisagreement(raw);
      const key = JSON.stringify([d.kind, d.claims, d.agents, d.evidence_ids, d.resolved, d.rule_id, d.resolution, d.question]);
      let entry = entries.get(key);
      if (!entry) entries.set(key, (entry = { view: d, rounds: [], final: false }));
      if (v.source === "run_record") entry.final = true;
      else if (!entry.rounds.includes(v.round)) entry.rounds.push(v.round);
    }
  }
  return [...entries.values()];
}

// --- the evidence registry ------------------------------------------------------------------------

export interface RegistryUse {
  /** The diagnosis' own check relied on it (basis_evidence_ids). */
  diagnosisBasis: boolean;
  /** Some passing rule of some verification relied on it (in_rule_basis). */
  ruleBasis: boolean;
  /** Claim ids citing it as support, and against. */
  supports: string[];
  contradicts: string[];
}

export function registryUse(item: Pick<EvidenceItem, "id" | "in_rule_basis" | "supports" | "contradicts">, diagnosis: CheckView | null): RegistryUse {
  return {
    diagnosisBasis: !!diagnosis?.basis_evidence_ids.includes(item.id),
    ruleBasis: item.in_rule_basis,
    supports: unique(item.supports),
    contradicts: unique(item.contradicts),
  };
}

/** How the registry filter "use" reads an item. */
export function registryUseKey(use: RegistryUse): "diagnosis basis" | "rule basis" | "cited" | "not cited" {
  if (use.diagnosisBasis) return "diagnosis basis";
  if (use.ruleBasis) return "rule basis";
  return use.supports.length || use.contradicts.length ? "cited" : "not cited";
}

/** A DOM id for an evidence row or a claim (anchors for the links between them). */
export function domId(prefix: "ev" | "claim", id: string): string {
  return `${prefix}-${id.replace(/[^A-Za-z0-9_-]/g, "_")}`;
}

/** A line of text split around the evidence ids it names (only ids in `known`), for linking. */
export function splitEvidenceIds(line: string, known: ReadonlySet<string>): ({ text: string } | { id: string })[] {
  const parts: ({ text: string } | { id: string })[] = [];
  let last = 0;
  for (const match of line.matchAll(/[A-Za-z0-9][A-Za-z0-9_.:-]*[A-Za-z0-9]/g)) {
    const token = match[0];
    const at = match.index ?? 0;
    if (!known.has(token)) continue;
    if (at > last) parts.push({ text: line.slice(last, at) });
    parts.push({ id: token });
    last = at + token.length;
  }
  if (last < line.length || !parts.length) parts.push({ text: line.slice(last) });
  return parts;
}

/** The evidence-gate checks (VP1..VP4) recorded for one proposal, in trace order. */
export function proposalChecksOf(checks: readonly ProposalCheckView[], proposalId: string): ProposalCheckView[] {
  return checks.filter((c) => c.proposal_id === proposalId).sort((a, b) => a.seq - b.seq);
}

/** The policy verdicts on a tool (a policy verdict names its tool, not a proposal). */
export function policyVerdictsOf(verdicts: readonly PolicyVerdictView[], tool: string): PolicyVerdictView[] {
  return verdicts.filter((v) => v.tool === tool);
}

/** The deciding rule of a policy verdict: the last rule id it lists. */
export function decidingRule(ruleIds: readonly string[]): string | null {
  return ruleIds.length ? ruleIds[ruleIds.length - 1] : null;
}

/** Evidence-gate rules (recap/agents/verifier.py PROPOSAL_RULES) for tooltips. */
export const PROPOSAL_RULES: Record<string, string> = {
  "VP1-evidence-exists": "the fix cites evidence recorded in this run",
  "VP2-target-matches-diagnosis": "the fix targets the diagnosed service",
  "VP3-action-named": "the fix names an action from the remediation catalog",
  "VP4-restore-point": "a fix that restores a version restores one the evidence names",
};

/** What a rule id means (verifier rules, evidence-gate rules); undefined when unknown. */
export function anyRuleNote(id: string): string | undefined {
  return VERIFIER_RULES[id] ?? PROPOSAL_RULES[id] ?? (id === "V1-consistency" ? "details that belong to another mechanism are reported, never used" : undefined);
}

/**
 * Per failed rule line of a check: true when it repeats a release or dependency the claim named
 * that is not shaped like one (older runs quoted it; the verifier now writes "(unnamed)"). The
 * line is RECAP's sentence, but that part is the provider's text, so the page tags it.
 */
export function quotedRuleLines(
  check: Pick<CheckView, "mechanism" | "detail" | "failed">,
  details?: Record<string, unknown> | null,
): boolean[] {
  const named = claimDetailOf({ mechanism: check.mechanism, details: details ?? {} });
  const values = unique([named, check.detail.trim()]).filter((v) => v && !isCheckedValue(check.mechanism, v));
  return check.failed.map((line) => values.some((value) => repeats(line, value)));
}

/** Open questions a verification left for the next round (its open disagreements' questions). */
export function openQuestionsOf(v: Pick<VerificationView, "disagreements">): string[] {
  return unique(v.disagreements.map(readDisagreement).filter((d) => !d.resolved && d.question).map((d) => d.question));
}

/** True when a value has the shape of a service name (a DNS label): shown as a checked value, else as quoted data. */
export function isServiceName(value: string | null | undefined): boolean {
  return typeof value === "string" && SERVICE_SHAPE.test(value);
}

/**
 * The text of a JSON object or array with indentation added, for reading; null when the text is
 * not JSON. Only whitespace changes: values, keys and their order stay as recorded.
 */
export function prettyJson(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed || (trimmed[0] !== "{" && trimmed[0] !== "[")) return null;
  try {
    const value: unknown = JSON.parse(trimmed);
    return value !== null && typeof value === "object" ? JSON.stringify(value, null, 2) : null;
  } catch {
    return null;
  }
}

/**
 * False when the run was verified before RECAP recorded proposers and disagreements (M3): every
 * check of its reports lacks proposers, and the API serves the missing lists as [].
 */
export function recordsDisagreements(view: Pick<EvidenceView, "verifications" | "final_report">): boolean {
  const checks = [...view.verifications, ...(view.final_report ? [view.final_report] : [])].flatMap((v) => v.checks);
  return !checks.length || checks.some((c) => c.proposers.length > 0);
}

/** The run's text author (every finding and proposal of a run has the same provider). */
export function runAuthorKind(view: Pick<EvidenceView, "findings" | "proposals">): AuthorKind | null {
  return view.findings[0]?.author ?? view.proposals[0]?.author ?? null;
}
