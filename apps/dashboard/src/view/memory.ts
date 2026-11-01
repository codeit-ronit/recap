/**
 * Pure helpers for the Experience Memory page (src/pages/Memory.tsx): partitions in plain
 * words, the red-team plant marker, an episode's consolidator-v2 run summary, and how a
 * run's memory hits were found and judged (signals, conditions, restore points).
 *
 * Runs and episodes recorded before M3 lack the new fields. Read back through the API they
 * arrive as empty defaults ([], {}, null, and summary_method "deterministic-extraction"),
 * so an empty value is never taken as a recorded fact: these helpers say "not recorded"
 * instead of guessing.
 *
 * This module imports types only, so the Node tests in ../../tests can load it directly.
 */

import type { IconName } from "../components/Icon";
import type { MemoryAccessView, MemoryHitView, MemoryRetrievalView } from "../dto";
import type { Tone } from "../format";
import type {
  ActionRecord,
  ExperienceEpisode,
  MemoryAssessPayload,
  MemoryHit,
  MemorySearchPayload,
  ProviderOrigin,
  TraceEvent,
} from "../types";

// --- defensive readers (episodes come from a file on disk, payloads from older runs) -------

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function has(value: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

/** Any value as text ("" for null or undefined); objects as JSON. */
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
  return Array.isArray(value) ? value.map(text) : [];
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

// --- partitions ------------------------------------------------------------------------------

export interface PartitionBadge {
  /** e.g. "live · real-model runs" */
  label: string;
  tone: Tone;
  title: string;
}

/** Who writes each partition (MemoryAccess.for_origin and the evaluation, ADR 0023). */
const PARTITIONS = new Map<string, { words: string; tone: Tone; title: string }>([
  [
    "demo",
    {
      words: "scripted runs (and real-model runs before M3)",
      tone: "neutral",
      title: "Scripted runs write here by default. Before M3, real-model runs did too.",
    },
  ],
  ["live", { words: "real-model runs", tone: "accent", title: "Runs whose decisions came from a real language model write here (since M3)." }],
  ["train", { words: "evaluation training", tone: "info", title: "The evaluation's training runs write here; its scored test runs read only this partition." }],
  ["test", { words: "held-out test", tone: "warn", title: "Held-out test runs never write memory: an episode here is unexpected." }],
]);

/** A partition in plain words, for a partition a run reads (not one episode: see episodePartitionBadge). */
export function partitionBadge(partition: string | null | undefined): PartitionBadge {
  if (!partition) return { label: "partition not recorded", tone: "neutral", title: "This episode did not record its partition." };
  const known = PARTITIONS.get(partition);
  if (!known) return { label: partition, tone: "neutral", title: "A partition this dashboard does not know." };
  return { label: `${partition} · ${known.words}`, tone: known.tone, title: known.title };
}

const SCRIPTED_PROVIDER = "deterministic-scripted-policy";

/**
 * One episode's partition, worded by who made its decisions. Before M3 every run wrote DEMO,
 * real-model runs included, so a demo episode says "scripted" only when its provenance does
 * (its origin, or before origins were recorded, the scripted policy as its provider). A
 * red-team plant says it was planted, whatever partition it claims.
 */
export function episodePartitionBadge(episode: Pick<ExperienceEpisode, "partition" | "provenance">): PartitionBadge {
  const partition = text(episode.partition);
  if (isPlanted(episode)) {
    return {
      label: `${partition || "partition not recorded"} · planted`,
      tone: "bad",
      title: "A red-team plant: no run RECAP recorded wrote this episode into this partition.",
    };
  }
  if (partition !== "demo") return partitionBadge(partition);
  const provenance = isRecord(episode.provenance) ? episode.provenance : {};
  const origin = text(provenance.origin).trim();
  const provider = text(provenance.provider).trim();
  if (origin === "scripted" || (!origin && provider.startsWith(SCRIPTED_PROVIDER))) {
    return { label: "demo · scripted run", tone: "neutral", title: "Written by a scripted run (the scripted policy: not a model)." };
  }
  if (origin) {
    return { label: "demo · real-model run", tone: "accent", title: `Its decisions came from ${origin}, although real-model runs write LIVE since M3.` };
  }
  if (provider) {
    return {
      label: "demo · real-model run (before M3)",
      tone: "accent",
      title: `Written before M3, when every run wrote DEMO; its provider was ${provider}.`,
    };
  }
  return { label: "demo · origin not recorded", tone: "neutral", title: "Before M3 both scripted and real-model runs wrote DEMO; this episode does not say which." };
}

// --- red-team plants -------------------------------------------------------------------------

/**
 * The provenance note of a red-team plant (scripts/redteam_setup.py), or null for any other
 * episode. A plant's provenance has "planted"; its value is fixture text, shown as data.
 */
export function plantedNote(episode: Pick<ExperienceEpisode, "provenance"> | null | undefined): string | null {
  const provenance = episode && isRecord(episode.provenance) ? episode.provenance : null;
  if (!provenance || !has(provenance, "planted")) return null;
  return text(provenance.planted) || "(no note)";
}

export function isPlanted(episode: Pick<ExperienceEpisode, "provenance"> | null | undefined): boolean {
  return plantedNote(episode) !== null;
}

// --- consolidator-v2: an episode's run summary -------------------------------------------------

/** Where a run summary came from: RECAP's extraction, a plant, or nowhere (older episodes). */
export type SummarySource = "extracted" | "planted" | "not_recorded";

export interface TrajectoryRow {
  agent: string | null;
  call: string;
  evidenceId: string;
  /** "tool" (a tool result) or "doc" (a runbook search) */
  kind: string;
}

export interface ClaimRow {
  mechanism: string;
  target: string | null;
  status: string;
  proposers: string[];
  ruleIds: string[];
  /** The rules' failed lines: RECAP's sentences, which may repeat a value the claim named. */
  failed: string[];
  /** The release or dependency that told this claim apart (ClaimRecord.detail); "" when none or not recorded. */
  detail: string;
  /** False when that detail is not shaped like a version or a service name: the claim's own text. */
  detailChecked: boolean;
  /** Per failed line: it repeats that unshaped detail, so part of RECAP's line is the claim's text. */
  failedQuotesClaim: boolean[];
}

// Values of these shapes are not free text (a version as the verifier reads it, a service name).
const VERSION_SHAPE = /^[vV]?[0-9]+\.[0-9]+\.[0-9]+$/;
const SERVICE_SHAPE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

/** True when `line` repeats `value` as a whole (not inside a longer word). */
function repeats(line: string, value: string): boolean {
  for (let at = line.indexOf(value); at >= 0; at = line.indexOf(value, at + 1)) {
    if (!/\w/.test(line.charAt(at - 1)) && !/\w/.test(line.charAt(at + value.length))) return true;
  }
  return false;
}

/**
 * A checked claim as the merger names it: "bad deployment on checkout (v2.4.1)", or
 * "dependency failure on cart (-> payment)". A detail not shaped like a checked value is left
 * out: the page shows it apart, as the claim's own text.
 */
export function claimWords(claim: Pick<ClaimRow, "mechanism" | "target" | "detail" | "detailChecked">): string {
  const words = `${(claim.mechanism || "unknown").replace(/_/g, " ")} on ${claim.target ?? "?"}`;
  if (!claim.detail || !claim.detailChecked) return words;
  return claim.mechanism === "dependency_failure" ? `${words} (-> ${claim.detail})` : `${words} (${claim.detail})`;
}

export interface EpisodeSummary {
  source: SummarySource;
  /** summary_method in plain words; known=false for a method this page does not know. */
  method: { text: string; known: boolean } | null;
  /** provenance.created_by, e.g. "consolidator-v2" */
  createdBy: string | null;
  trajectory: TrajectoryRow[];
  claims: ClaimRow[];
  basis: string[];
  /** [key, value]: action, target, mechanism, bad_version, to_version first, then any other. */
  preconditions: [string, string][];
}

const SUMMARY_METHODS = new Map([["deterministic-extraction", "deterministic extraction from RECAP's own records"]]);
const PRECONDITION_ORDER = ["action", "target", "mechanism", "bad_version", "to_version"];

export function summaryMethod(value: unknown): { text: string; known: boolean } | null {
  const raw = text(value).trim();
  if (!raw) return null;
  const words = SUMMARY_METHODS.get(raw);
  return words ? { text: words, known: true } : { text: raw, known: false };
}

function createdBy(episode: ExperienceEpisode): string | null {
  const provenance = isRecord(episode.provenance) ? episode.provenance : {};
  return text(provenance.created_by).trim() || null;
}

/** consolidator-v2 and later record the summary fields, even when they are empty. */
function writesSummary(consolidator: string | null): boolean {
  const match = /^consolidator-v(\d+)$/.exec(consolidator ?? "");
  return match !== null && Number(match[1]) >= 2;
}

export function episodeSummary(episode: ExperienceEpisode): EpisodeSummary {
  const by = createdBy(episode);
  const trajectory = records(episode.trajectory).map((step) => ({
    agent: text(step.agent) || null,
    call: text(step.call),
    evidenceId: text(step.evidence_id),
    kind: text(step.kind) || "tool",
  }));
  const claims = records(episode.checked_claims).map((claim) => {
    const mechanism = text(claim.mechanism);
    const detail = text(claim.detail).trim();
    const shape = mechanism === "dependency_failure" ? SERVICE_SHAPE : VERSION_SHAPE;
    const failed = texts(claim.failed);
    const checked = !detail || shape.test(detail);
    return {
      mechanism,
      target: text(claim.target) || null,
      status: text(claim.status),
      proposers: texts(claim.proposers),
      ruleIds: texts(claim.rule_ids),
      failed,
      detail,
      detailChecked: checked,
      failedQuotesClaim: failed.map((line) => !checked && repeats(line, detail)),
    };
  });
  const basis = texts(episode.diagnosis_basis);
  const fix = isRecord(episode.fix_preconditions) ? episode.fix_preconditions : {};
  const keys = [...PRECONDITION_ORDER.filter((key) => has(fix, key)), ...Object.keys(fix).filter((key) => !PRECONDITION_ORDER.includes(key))];
  const preconditions = keys.map((key): [string, string] => [key, text(fix[key])]);

  // A plant's summary_method is only the field's default (pydantic fills it in): nothing extracted it.
  const content = trajectory.length + claims.length + basis.length + preconditions.length > 0;
  const source: SummarySource = isPlanted(episode) ? "planted" : writesSummary(by) || content ? "extracted" : "not_recorded";
  return {
    source,
    method: source === "extracted" ? summaryMethod(episode.summary_method) : null,
    createdBy: by,
    trajectory,
    claims,
    basis,
    preconditions,
  };
}

// --- who made the past run's decisions (consolidator-v2 provenance) ----------------------------

export type FieldState = "value" | "none" | "not_recorded";

export interface FieldRow {
  key: string;
  label: string;
  value: string | null;
  state: FieldState;
}

const DECISION_FIELDS: [string, string][] = [
  ["origin", "Decisions by"],
  ["model", "Model"],
  ["prompt_version", "Prompt version"],
  ["weights", "Model weights"],
  ["retrieval", "Runbook retrieval"],
  ["code_version", "Code version"],
];

/**
 * The provenance fields consolidator-v2 adds, plus the incident's embedder. A key the
 * episode lacks is "not recorded"; a key it holds as null or "" is "none".
 */
export function decisionRows(episode: ExperienceEpisode): FieldRow[] {
  const provenance = isRecord(episode.provenance) ? episode.provenance : {};
  const rows = DECISION_FIELDS.map(([key, label]): FieldRow => {
    if (!has(provenance, key)) return { key, label, value: null, state: "not_recorded" };
    const value = text(provenance[key]).trim();
    return value ? { key, label, value, state: "value" } : { key, label, value: null, state: "none" };
  });
  // The API fills a missing embedding_model with null, so null means "none" only on a v2 episode.
  const embedder = text(episode.embedding_model).trim();
  const empty: FieldState = writesSummary(createdBy(episode)) ? "none" : "not_recorded";
  rows.push({ key: "embedding_model", label: "Incident embedded by", value: embedder || null, state: embedder ? "value" : empty });
  return rows;
}

const ORIGINS: readonly string[] = ["scripted", "local_llm", "free_hosted_llm", "paid_llm", "replay"];

/** provenance.origin when it is a known run origin (for the origin badge), else undefined. */
export function episodeOrigin(episode: ExperienceEpisode): ProviderOrigin | undefined {
  const origin = isRecord(episode.provenance) ? episode.provenance.origin : undefined;
  return typeof origin === "string" && ORIGINS.includes(origin) ? (origin as ProviderOrigin) : undefined;
}

// --- memory hits: how each was found and judged -----------------------------------------------

export interface HitSignals {
  /** Word overlap: the `similarity` that routing and the scripted policy use. */
  lexical: number | null;
  /** Cosine of the two incident embeddings, when the run embedded with the episode's model. */
  semantic: number | null;
  /** False for runs before M3: they recorded no signals. */
  recorded: boolean;
  /** Any other signal, formatted. */
  other: [string, string][];
}

export function hitSignals(hit: MemoryHit): HitSignals {
  const signals = isRecord(hit.signals) ? hit.signals : {};
  const keys = Object.keys(signals);
  return {
    lexical: finite(hit.similarity) ?? finite(signals.lexical),
    semantic: finite(signals.semantic),
    recorded: keys.length > 0,
    other: keys.filter((key) => key !== "lexical" && key !== "semantic").map((key): [string, string] => [key, fmtSignal(key, signals[key])]),
  };
}

/** A signal value: ranks arrive as floats (12.0) and print as integers; scores get 2 decimals. */
export function fmtSignal(key: string, value: unknown): string {
  const n = finite(value);
  if (n === null) return "—";
  return /(^|_)rank$/.test(key) ? String(Math.round(n)) : n.toFixed(2);
}

export interface HitConditions {
  /** Conditions that held (MemoryHit.supporting). */
  held: string[];
  /** Conditions against reuse (MemoryHit.contradicting). */
  against: string[];
  /** The M3 reuse check ran: it always records at least one condition. */
  recorded: boolean;
}

export function hitConditions(hit: MemoryHit): HitConditions {
  const held = texts(hit.supporting);
  const against = texts(hit.contradicting);
  return { held, against, recorded: held.length + against.length > 0 };
}

export type HitOrder = "ranked" | "as_recorded" | "search";

/**
 * How a run's hits are ordered. The M3 check re-ranks them by what live evidence confirmed
 * (rank_hits); before any check they are in search order; a run checked before M3 keeps
 * whatever order it recorded.
 */
export function hitOrder(hits: readonly MemoryHit[]): HitOrder | null {
  if (!hits.length) return null;
  if (hits.some((hit) => hitConditions(hit).recorded)) return "ranked";
  if (hits.some((hit) => typeof hit.mechanism_match === "boolean")) return "as_recorded";
  return "search";
}

export const HIT_ORDER_NOTES: Record<HitOrder, string> = {
  ranked: "Ranked by what live evidence confirmed: safe to reuse first, then a matching mechanism, then similarity.",
  as_recorded: "In the order this run recorded them (it was checked before M3, which re-ranks by live evidence).",
  search: "Most similar first: not yet checked against live evidence.",
};

// --- the run's trace: memory.search and memory.assessment --------------------------------------

export type RestoreState = "named" | "none_named" | "not_looked_up" | "not_recorded" | "not_checked";

export interface MemoryTrace {
  /** A memory.search event exists (memory was on). */
  searched: boolean;
  readable: string[];
  /** The semantic signal's embedder; null: the run did not embed; undefined: not recorded. */
  embedder: string | null | undefined;
  /**
   * A replay reads memory as it was when its recording began: the episodes created before this
   * time (memory.search created_before). null: the live memory; undefined: not recorded.
   */
  asOf: string | null | undefined;
  /** memory.assessment events: one per verification of live evidence. */
  checks: number;
  /** restore_points of the latest check. */
  restore: RestoreState;
  restorePoints: string[];
}

export function memoryTrace(events: readonly TraceEvent[]): MemoryTrace {
  let search: MemorySearchPayload | null = null;
  let assessment: MemoryAssessPayload | null = null;
  let searchSeq = -Infinity;
  let assessmentSeq = -Infinity;
  let checks = 0;
  // The latest event of each kind wins (events arrive in seq order, but do not rely on it).
  for (const event of events) {
    const payload = isRecord(event.payload) ? event.payload : {};
    const seq = finite(event.seq) ?? 0;
    if (event.category === "memory.search" && seq >= searchSeq) {
      search = payload as MemorySearchPayload;
      searchSeq = seq;
    } else if (event.category === "memory.assessment") {
      checks += 1;
      if (seq >= assessmentSeq) {
        assessment = payload as MemoryAssessPayload;
        assessmentSeq = seq;
      }
    }
  }

  let restore: RestoreState = "not_checked";
  let restorePoints: string[] = [];
  if (assessment) {
    const points: unknown = assessment.restore_points;
    if (!has(assessment, "restore_points")) restore = "not_recorded";
    else if (points === null) restore = "not_looked_up";
    else if (Array.isArray(points)) {
      restorePoints = texts(points);
      restore = restorePoints.length ? "named" : "none_named";
    } else restore = "not_recorded";
  }
  const embedder = search && has(search, "semantic") ? text(search.semantic).trim() || null : undefined;
  const asOf = search && has(search, "created_before") ? text(search.created_before).trim() || null : undefined;
  return { searched: search !== null, readable: texts(search?.readable_partitions), embedder, asOf, checks, restore, restorePoints };
}

export const RESTORE_NOTES: Record<Exclude<RestoreState, "named">, string> = {
  none_named: "none: today's evidence names no restore point",
  not_looked_up: "not looked up: the latest check had no supported diagnosis naming a service",
  not_recorded: "not recorded (this run was checked before M3)",
  not_checked: "no live check yet",
};

// =============================================================================================
// M4: episodes grouped by partition, who reads and writes memory (GET /system), each episode's
// source run and past action, and one run's memory as GET /runs/{id}/retrieval reports it.
// =============================================================================================

/** The partitions in the order the page groups them (any other partition follows). */
export const PARTITION_ORDER: readonly string[] = ["demo", "live", "train", "test"];

export interface PartitionInfo {
  partition: string;
  /** Who writes it, in a few words ("real-model runs"). */
  words: string;
  /** One plain sentence for a newcomer. */
  line: string;
  /** Why the partition has no episode (shown when it is empty). */
  empty: string;
  tone: Tone;
}

const PARTITION_LINES = new Map<string, { line: string; empty: string }>([
  [
    "demo",
    {
      line: "Scripted runs write here. Before M3 real-model runs did too, so each episode says who made its decisions.",
      empty: "No scripted run on this server has written an episode yet.",
    },
  ],
  [
    "live",
    {
      line: "Real-model runs write here (since M3). Replays and held-out test scenarios never write.",
      empty: "No real-model run on this server has written an episode yet.",
    },
  ],
  [
    "train",
    {
      line: "Evaluation training runs write here; scored evaluation runs read only this partition.",
      empty: "No evaluation training run has written an episode into this memory.",
    },
  ],
  [
    "test",
    {
      line: "Held-out evaluation runs never write memory, so this partition stays empty by design.",
      empty: "Empty by design: held-out test runs never write memory.",
    },
  ],
]);

export function partitionInfo(partition: string): PartitionInfo {
  const known = PARTITIONS.get(partition);
  const lines = PARTITION_LINES.get(partition);
  return {
    partition,
    words: known?.words ?? "a partition this dashboard does not know",
    line: lines?.line ?? "The server lists this partition; this dashboard has no description of it.",
    empty: lines?.empty ?? "No episode in this partition.",
    tone: known?.tone ?? "neutral",
  };
}

export interface PartitionGroup extends PartitionInfo {
  episodes: ExperienceEpisode[];
  /** Red-team plants among them (false memories put there on purpose). */
  plants: number;
}

/**
 * Episodes grouped by partition: demo, live, train, test first (always, so an empty partition
 * says why it is empty), then any other partition the server lists or an episode names.
 * Within a group, the server's order is kept.
 */
export function groupEpisodes(episodes: readonly ExperienceEpisode[], partitions?: readonly string[] | null): PartitionGroup[] {
  const order = [...PARTITION_ORDER];
  for (const name of [...(partitions ?? []), ...episodes.map((e) => text(e.partition) || "not recorded")]) {
    if (!order.includes(name)) order.push(name);
  }
  return order.map((name) => {
    const members = episodes.filter((e) => (text(e.partition) || "not recorded") === name);
    return { ...partitionInfo(name), episodes: members, plants: members.filter(isPlanted).length };
  });
}

// --- who reads and writes which partition ---------------------------------------------------------

export interface AccessRule {
  key: string;
  /** The kind of run, in words. */
  who: string;
  read: string[];
  /** null: this kind of run never writes memory. */
  write: string | null;
  note: string | null;
  /** "server": GET /system reports it now; "code": the evaluation's fixed rule (documentation). */
  source: "server" | "code";
}

const ACCESS_WHO: [string, string, string | null][] = [
  ["scripted", "Scripted baseline run", null],
  ["local_llm", "Local LLM run", null],
  ["free_hosted_llm", "Free hosted LLM run", null],
  ["paid_llm", "Paid LLM run", null],
  ["replay", "Replay of a recorded run", "reads memory as it was when its recording began"],
];

/** The evaluation's own access (MemoryAccess.eval_train / eval_test, ADR 0023): fixed in code. */
export const EVALUATION_ACCESS: readonly AccessRule[] = [
  { key: "eval_train", who: "Evaluation training run", read: ["train"], write: "train", note: null, source: "code" },
  {
    key: "eval_test",
    who: "Evaluation test run (scored)",
    read: ["train"],
    write: null,
    note: "held-out scenarios never become episodes",
    source: "code",
  },
];

/** GET /system features.memory_access as rows (known run kinds first), then the evaluation's rules. */
export function accessRules(access: Record<string, MemoryAccessView> | null | undefined): AccessRule[] {
  const rows: AccessRule[] = [];
  const known = new Set<string>();
  const add = (key: string, who: string, note: string | null) => {
    const rule = access?.[key];
    if (!rule || !isRecord(rule)) return;
    known.add(key);
    rows.push({ key, who, read: texts(rule.read), write: text(rule.write) || null, note, source: "server" });
  };
  for (const [key, who, note] of ACCESS_WHO) add(key, who, note);
  for (const key of Object.keys(access ?? {}).sort()) if (!known.has(key)) add(key, `${key} run`, null);
  return [...rows, ...EVALUATION_ACCESS];
}

// --- an episode's source run, outcome and past action ------------------------------------------------

export type SourceRunState = "present" | "absent" | "unknown" | "planted";

export interface SourceRun {
  runId: string;
  state: SourceRunState;
}

/**
 * Whether the run that wrote an episode is on this server (it then links to its Trace Explorer).
 * `known`: the run ids this server listed, and whether that list is complete (null: not loaded).
 */
export function sourceRun(
  episode: Pick<ExperienceEpisode, "origin_run_id" | "provenance">,
  known: { ids: ReadonlySet<string>; complete: boolean } | null,
): SourceRun {
  const runId = text(episode.origin_run_id);
  if (isPlanted(episode)) return { runId, state: "planted" };
  if (!runId || !known) return { runId, state: "unknown" };
  if (known.ids.has(runId)) return { runId, state: "present" };
  return { runId, state: known.complete ? "absent" : "unknown" };
}

export interface OutcomeWords {
  label: string;
  tone: Tone;
}

/**
 * What happened in the one run an episode records, worded as that run's result (never as a
 * general claim about a model or the scripted policy). A plant's outcome is only its claim.
 */
export function episodeOutcome(episode: { outcome: string | null | undefined; verified: boolean | null | undefined }): OutcomeWords {
  switch (episode.outcome) {
    case "success":
      return episode.verified
        ? { label: "Recovered · oracle-verified", tone: "ok" }
        : { label: "Success claimed · not verified", tone: "warn" };
    case "failed":
      return { label: "Not recovered", tone: "bad" };
    case "blocked":
      return { label: "Blocked · fix not applied", tone: "bad" };
    case "budget_exhausted":
      return { label: "Stopped · budget exhausted", tone: "warn" };
    default:
      return { label: text(episode.outcome) ? text(episode.outcome).replace(/_/g, " ") : "Not recorded", tone: "neutral" };
  }
}

/**
 * Who made an episode's decisions when it records no origin (consolidator-v1, before M3): the
 * scripted policy when its provider says so, else a real model named by its provider.
 */
export function legacyDecider(episode: Pick<ExperienceEpisode, "provenance">): { words: string; provider: string | null } {
  const provenance = isRecord(episode.provenance) ? episode.provenance : {};
  const provider = text(provenance.provider).trim() || null;
  if (!provider) return { words: "Not recorded", provider: null };
  if (provider.startsWith(SCRIPTED_PROVIDER)) return { words: "scripted policy (not an LLM)", provider };
  return { words: "a real model, recorded before M3", provider };
}

/** "execute_rollback on checkout (to_version=v2.4.0)", or null when there is no action. */
export function actionWords(action: Pick<ActionRecord, "action" | "target" | "params"> | null | undefined): string | null {
  if (!action || !isRecord(action)) return null;
  const params = isRecord(action.params)
    ? Object.entries(action.params)
        .filter(([, value]) => value !== null && value !== undefined)
        .map(([key, value]) => `${key}=${text(value)}`)
        .join(", ")
    : "";
  const name = text(action.action) || "unknown action";
  const target = text(action.target);
  return `${name}${target ? ` on ${target}` : ""}${params ? ` (${params})` : ""}`;
}

/** "bad deployment on checkout", or null when the run reached no verified diagnosis. */
export function diagnosisWords(episode: Pick<ExperienceEpisode, "failure_mechanism" | "mechanism_target">): string | null {
  const mechanism = text(episode.failure_mechanism);
  if (!mechanism) return null;
  return `${mechanism.replace(/_/g, " ")} on ${text(episode.mechanism_target) || "an unnamed service"}`;
}

/** The rule every reuse must pass (assess_reuse, ADR 0023), in plain words: documentation. */
export const REUSE_RULE: readonly string[] = [
  "the past run recovered, and the independent oracle verified it",
  "today's evidence supports the same mechanism on the same service",
  "a remembered rollback restores a version today's evidence names, never today's bad release",
];

// --- one run's memory (GET /runs/{id}/retrieval `memory`) ----------------------------------------------

export type WriteState = "written" | "skipped" | "disabled" | "pending" | "not_recorded";

export interface RunMemoryFacts {
  enabled: boolean;
  /** The partitions the run read: its search's, else its configuration's. */
  read: string[];
  /** A replay reads memory as of its recording's start (the search's created_before). */
  asOf: string | null;
  /** The embedder of a semantic search, when one ran. */
  semantic: string | null;
  searches: number;
  /** memory.assessment events: live checks of the hits. */
  checks: number;
  write: { state: WriteState; episodeId: string | null; partition: string | null; reasons: string[] };
}

export function runMemoryFacts(memory: MemoryRetrievalView, terminal: boolean): RunMemoryFacts {
  const searched = memory.searches.find((s) => s.readable_partitions.length) ?? null;
  const read = searched ? texts(searched.readable_partitions) : texts(memory.read_partitions);
  const asOf = memory.searches.map((s) => text(s.created_before)).find(Boolean) ?? null;
  const semantic = memory.searches.map((s) => text(s.semantic)).find(Boolean) ?? null;
  const reasons = texts(memory.skipped);
  let write: RunMemoryFacts["write"];
  if (memory.written && text(memory.written.episode_id)) {
    write = { state: "written", episodeId: text(memory.written.episode_id), partition: text(memory.written.partition) || null, reasons: [] };
  } else if (reasons.length) write = { state: "skipped", episodeId: null, partition: null, reasons };
  else if (!memory.write_enabled) write = { state: "disabled", episodeId: null, partition: null, reasons: [] };
  else if (!terminal) write = { state: "pending", episodeId: null, partition: text(memory.write_partition) || null, reasons: [] };
  else write = { state: "not_recorded", episodeId: null, partition: text(memory.write_partition) || null, reasons: [] };
  return { enabled: memory.enabled, read, asOf, semantic, searches: memory.searches.length, checks: memory.assessments.length, write };
}

export type ReuseKind = "safe" | "rejected" | "not_checked";

export interface ReuseView {
  label: string;
  tone: Tone;
  icon: IconName;
  meaning: string;
}

export const REUSE_VIEWS: Readonly<Record<ReuseKind, ReuseView>> = {
  safe: { label: "Safe to reuse", tone: "ok", icon: "check", meaning: "Every reuse condition held on this run's own evidence." },
  rejected: { label: "Rejected", tone: "bad", icon: "x", meaning: "RECAP's reuse check found at least one condition against reuse." },
  not_checked: {
    label: "Not checked",
    tone: "neutral",
    icon: "minus",
    meaning: "No live check covered this hit, so it was never treated as safe to reuse.",
  },
};

export interface HitCheck {
  seq: number;
  round: number | null;
  safe: boolean | null;
  mechanismMatch: boolean | null;
  reason: string | null;
  held: string[];
  against: string[];
  /** Versions today's evidence named as restore points ([]: none; null: none looked up). */
  restorePoints: string[] | null;
}

export interface RunHit {
  episodeId: string;
  lexical: number | null;
  semantic: number | null;
  otherSignals: [string, string][];
  /** False when the run recorded no signals (before M3). */
  signalsRecorded: boolean;
  partition: string | null;
  /** The past run's result, as the hit recorded it. */
  outcome: string | null;
  verified: boolean | null;
  mechanism: string | null;
  service: string | null;
  reuse: ReuseKind;
  mechanismMatch: boolean | null;
  /** RECAP's reuse verdict in its own words. */
  reason: string | null;
  held: string[];
  against: string[];
  /** Every live check of this hit, oldest first. */
  checks: HitCheck[];
  /** The full episode, when the run record or GET /memory has it. */
  episode: ExperienceEpisode | null;
  planted: boolean;
}

function hitCheck(seq: number, round: number | null, hit: MemoryHitView, restore: unknown): HitCheck {
  return {
    seq,
    round,
    safe: typeof hit.safe_to_reuse === "boolean" ? hit.safe_to_reuse : null,
    mechanismMatch: typeof hit.mechanism_match === "boolean" ? hit.mechanism_match : null,
    reason: text(hit.reason) || null,
    held: texts(hit.supporting),
    against: texts(hit.contradicting),
    restorePoints: Array.isArray(restore) ? texts(restore) : null,
  };
}

/**
 * The run's memory hits with RECAP's reuse verdicts: the hits as the run ended (else its latest
 * search's), each with every live check that covered it. A hit no check covered is "not
 * checked", whatever default the record holds.
 */
export function runHits(memory: MemoryRetrievalView, episodes: ReadonlyMap<string, ExperienceEpisode>): RunHit[] {
  const lastSearch = memory.searches.length ? memory.searches[memory.searches.length - 1] : null;
  const base = memory.final_hits.length ? memory.final_hits : (lastSearch?.hits ?? []);
  const searchHit = (id: string) => {
    for (let i = memory.searches.length - 1; i >= 0; i -= 1) {
      const found = memory.searches[i].hits.find((h) => h.episode_id === id);
      if (found) return found;
    }
    return null;
  };
  return base.map((hit) => {
    const id = text(hit.episode_id);
    const checks = memory.assessments
      .flatMap((a) => a.hits.filter((h) => h.episode_id === id).map((h) => hitCheck(a.seq, a.round, h, a.restore_points)))
      .sort((a, b) => a.seq - b.seq);
    const latest = checks.length ? checks[checks.length - 1] : null;
    // Without any assessment event (a trace that was lost), the record's own verdict counts.
    const assessedByRecord =
      memory.assessments.length === 0 && (typeof hit.mechanism_match === "boolean" || hit.supporting.length + hit.contradicting.length > 0);
    const safe = typeof hit.safe_to_reuse === "boolean" ? hit.safe_to_reuse : (latest?.safe ?? null);
    const reuse: ReuseKind = !checks.length && !assessedByRecord ? "not_checked" : safe === true ? "safe" : "rejected";
    const found = searchHit(id);
    const signals = isRecord(hit.signals) && Object.keys(hit.signals).length ? hit.signals : isRecord(found?.signals) ? found.signals : {};
    const keys = Object.keys(signals);
    const episode = episodes.get(id) ?? null;
    const held = texts(hit.supporting).length || texts(hit.contradicting).length ? texts(hit.supporting) : (latest?.held ?? []);
    const against = texts(hit.supporting).length || texts(hit.contradicting).length ? texts(hit.contradicting) : (latest?.against ?? []);
    return {
      episodeId: id,
      lexical: finite(signals.lexical) ?? finite(hit.similarity) ?? finite(found?.similarity),
      semantic: finite(signals.semantic),
      otherSignals: keys.filter((key) => key !== "lexical" && key !== "semantic").map((key): [string, string] => [key, fmtSignal(key, signals[key])]),
      signalsRecorded: keys.length > 0,
      partition: text(hit.partition) || text(found?.partition) || (episode ? text(episode.partition) : "") || null,
      outcome: text(hit.outcome) || text(found?.outcome) || (episode ? text(episode.outcome) : "") || null,
      verified: typeof hit.verified === "boolean" ? hit.verified : typeof found?.verified === "boolean" ? found.verified : (episode?.verified ?? null),
      mechanism: text(hit.mechanism) || text(found?.mechanism) || (episode ? text(episode.failure_mechanism) : "") || null,
      service: text(hit.service) || text(found?.service) || (episode ? text(episode.service) : "") || null,
      reuse,
      mechanismMatch: typeof hit.mechanism_match === "boolean" ? hit.mechanism_match : (latest?.mechanismMatch ?? null),
      reason: text(hit.reason) || latest?.reason || null,
      held,
      against,
      checks,
      episode,
      planted: isPlanted(episode),
    };
  });
}

/** Episodes by id: the run record's hits first (as the run saw them), then GET /memory. */
export function episodeIndex(...lists: readonly (readonly ExperienceEpisode[] | null | undefined)[]): Map<string, ExperienceEpisode> {
  const index = new Map<string, ExperienceEpisode>();
  for (const list of lists) {
    for (const episode of list ?? []) {
      const id = text(episode?.episode_id);
      if (id && !index.has(id)) index.set(id, episode);
    }
  }
  return index;
}
