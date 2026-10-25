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

import type { Tone } from "../format";
import type {
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
