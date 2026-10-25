/**
 * View helpers for the retrieval panels of the Evidence & RAG page: how a run searched the
 * runbooks (RunRecord.retrieval), every search it traced (retrieval.docs / retrieval.skip) with
 * its ranking signals, knowledge overlays, and the receipts of search_runbooks calls.
 *
 * Pure functions with type-only imports, so the Node tests in ../../tests load this file
 * directly. Runs recorded before M3 lack most of these keys: a missing value reads MISSING.
 */

import type { AgentFinding, Evidence, RetrievalInfo, TraceEvent } from "../types";

/** What a value the run did not record reads as (the dashboard's dash, as in fmtInt). */
export const MISSING = "—";

// --- defensive readers (local copies: this module imports no runtime values) ------------------

function arr(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function fmtCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("en-US") : MISSING;
}

// --- numbers: ranks, scores and signals -----------------------------------------------------------

/** A rank arrives as a float (12.0) and is shown as an integer. */
export function fmtRank(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? String(Math.round(value)) : MISSING;
}

/** A score or signal: at most 4 significant digits, never padded with zeros it does not have. */
export function fmtMeasure(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? String(Number(value.toPrecision(4))) : MISSING;
}

/** The ranking signals a search may record, in table order (RetrievedChunk.signals). */
export const SIGNAL_COLUMNS: readonly { key: string; label: string; title: string }[] = [
  { key: "bm25_rank", label: "BM25 rank", title: "rank by keyword score (BM25)" },
  { key: "bm25", label: "BM25", title: "keyword score (BM25)" },
  { key: "dense_rank", label: "Dense rank", title: "rank by meaning (embedding similarity)" },
  { key: "cosine", label: "Cosine", title: "cosine similarity of the query and chunk embeddings" },
  { key: "rrf", label: "RRF", title: "reciprocal rank fusion of the two ranks: the fused score" },
];

/** One signal of a result; a ranking the search did not use reads MISSING. */
export function fmtSignal(signals: Readonly<Record<string, number>> | null | undefined, key: string): string {
  const value = signals?.[key];
  return key.endsWith("_rank") ? fmtRank(value) : fmtMeasure(value);
}

/** A chunk's signals per ranking it used, e.g. ["BM25 #12 (1.296)", "dense #1 (cosine 1)", "RRF 0.03028"]. */
export function signalParts(signals: Readonly<Record<string, number>> | null | undefined): string[] {
  if (!signals) return [];
  const has = (key: string) => num(signals[key]) !== null;
  const parts: string[] = [];
  if (has("bm25_rank") || has("bm25")) parts.push(`BM25 #${fmtRank(signals.bm25_rank)} (${fmtMeasure(signals.bm25)})`);
  if (has("dense_rank") || has("cosine")) parts.push(`dense #${fmtRank(signals.dense_rank)} (cosine ${fmtMeasure(signals.cosine)})`);
  if (has("rrf")) parts.push(`RRF ${fmtMeasure(signals.rrf)}`);
  return parts;
}

/** The same on one line, e.g. "BM25 #12 (1.296) · dense #1 (cosine 1) · RRF 0.03028". */
export function signalSummary(signals: Readonly<Record<string, number>> | null | undefined): string {
  return signalParts(signals).join(" · ") || MISSING;
}

/** Signals with numeric values only; null when the result recorded none (before M3). */
export function readSignals(raw: unknown): Record<string, number> | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const out: Record<string, number> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (typeof value === "number" && Number.isFinite(value)) out[key] = value;
  }
  return out;
}

// --- searches (retrieval.docs / retrieval.skip) ------------------------------------------------

export interface SearchResult {
  rank: number | null;
  chunkId: string;
  docPath: string; // absent from search receipts
  title: string; // receipts only
  heading: string;
  score: number | null;
  signals: Record<string, number> | null;
}

export interface EmbeddingCall {
  model: string;
  inputTokens: number | null; // as the server reported them
  durationMs: number | null;
  replayed: boolean;
}

export interface Search {
  seq: number;
  skipped: boolean;
  /** "initial", "proposal" or "agent" ("" when not recorded). */
  stage: string;
  /** The agent that searched (search_runbooks); null for RECAP's own searches. */
  agent: string | null;
  reason: string;
  query: string;
  /** Who wrote the query: an agent (untrusted text) or RECAP itself. */
  queryBy: "agent" | "recap";
  mode: string;
  retriever: string;
  embedding: EmbeddingCall | null;
  results: SearchResult[];
}

function readResult(raw: unknown): SearchResult {
  const r = record(raw);
  return {
    rank: num(r.rank),
    chunkId: text(r.chunk_id),
    docPath: text(r.doc_path),
    title: text(r.title),
    heading: text(r.heading),
    score: num(r.score),
    signals: readSignals(r.signals),
  };
}

function readEmbedding(raw: unknown): EmbeddingCall | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const e = record(raw);
  return { model: text(e.model), inputTokens: num(e.input_tokens), durationMs: num(e.duration_ms), replayed: e.replayed === true };
}

/** Every runbook search the run traced, in order, skipped ones included. */
export function searchesOf(events: readonly TraceEvent[]): Search[] {
  return events
    .filter((e) => e.category === "retrieval.docs" || e.category === "retrieval.skip")
    .map((e) => {
      const p = e.payload;
      const stage = text(p.stage);
      return {
        seq: e.seq,
        skipped: e.category === "retrieval.skip",
        stage,
        agent: e.agent ?? null,
        reason: text(p.reason),
        query: text(p.query),
        queryBy: stage === "agent" || e.agent ? "agent" : "recap",
        mode: text(p.mode),
        retriever: text(p.retriever),
        embedding: readEmbedding(p.embedding),
        results: arr(p.results).map(readResult),
      };
    });
}

export function stageLabel(stage: string): string {
  switch (stage) {
    case "initial":
      return "initial";
    case "proposal":
      return "proposal";
    case "agent":
      return "agent search";
    default:
      return stage || "stage not recorded";
  }
}

/** Where RECAP's own query came from (controller._retrieve and _maybe_retrieve_runbook). */
export function recapQueryNote(stage: string): string {
  switch (stage) {
    case "initial":
      return "RECAP's query: the alert's service and symptoms";
    case "proposal":
      return "RECAP's query: the diagnosed mechanism";
    default:
      return "RECAP's query";
  }
}

// --- RunRecord.retrieval --------------------------------------------------------------------------

export function modeNote(mode: string | null | undefined): string {
  switch (mode) {
    case "hybrid":
      return "BM25 keyword ranking fused with a dense ranking from a local embedding model (reciprocal rank fusion)";
    case "bm25":
      return "BM25 keyword ranking only";
    default:
      return "not recorded";
  }
}

/** "sha256:e3bee3bee3be…": a digest shortened to `n` hex characters, its algorithm kept. */
export function shortDigest(digest: string, n = 12): string {
  const match = /^([A-Za-z0-9]+):([0-9a-fA-F]+)$/.exec(digest);
  const [prefix, hex] = match ? [`${match[1]}:`, match[2]] : ["", digest];
  return hex.length > n ? `${prefix}${hex.slice(0, n)}…` : digest;
}

export interface RetrievalSummary {
  mode: string;
  modeNote: string;
  embedder: string;
  server: string;
  /** The full weights digest (for a tooltip). */
  weights: string | null;
  embeddingRequests: string;
  /** False when nothing was embedded (a BM25 run): replay counts then say nothing. */
  embedded: boolean;
  embeddingInputTokens: string;
  /** True only when the server reported a token count. */
  tokensReported: boolean;
  replayedRequests: string;
  agentSearches: string;
}

/** RunRecord.retrieval as display strings; null when the run did not record it (before M3). */
export function retrievalSummary(info: RetrievalInfo | null | undefined): RetrievalSummary | null {
  if (!info) return null;
  const server = record(info.server);
  const weights = text(server.weights) || null;
  const serverParts = [
    text(server.ollama_version) ? `Ollama ${text(server.ollama_version)}` : "",
    text(server.parameter_size) ? `${text(server.parameter_size)} parameters` : "",
    text(server.quantization),
    weights ? `weights ${shortDigest(weights)}` : "",
  ].filter(Boolean);
  const requests = num(info.embedding_requests);
  const tokens = num(info.embedding_input_tokens);
  return {
    mode: text(info.mode) || MISSING,
    modeNote: modeNote(info.mode),
    embedder: info.embedder ? info.embedder : info.mode === "bm25" ? "none (BM25 only)" : MISSING,
    server: serverParts.join(" · ") || MISSING,
    weights,
    embeddingRequests: fmtCount(requests),
    embedded: !!requests,
    // Tokens exist only as a server reported them: requests without a count are not zero.
    embeddingInputTokens: tokens !== null ? fmtCount(tokens) : requests ? "not reported by the server" : MISSING,
    tokensReported: tokens !== null,
    replayedRequests: fmtCount(num(info.replayed_requests)),
    agentSearches: fmtCount(num(info.agent_searches)),
  };
}

// --- search_runbooks receipts ----------------------------------------------------------------------

/** DOC evidence from search_runbooks: a receipt from RECAP's own index, never proof. */
export function isSearchReceipt(e: Pick<Evidence, "source_type" | "tool_name">): boolean {
  return e.source_type === "doc" && e.tool_name === "search_runbooks";
}

export interface SearchReceipt {
  /** Written by the agent that searched: untrusted text. */
  query: string;
  mode: string;
  results: SearchResult[];
}

export function receiptOf(e: Evidence): SearchReceipt | null {
  if (!isSearchReceipt(e)) return null;
  const d = record(e.data);
  return { query: text(d.query), mode: text(d.mode), results: arr(d.results).map(readResult) };
}

// --- knowledge overlays (red team) and citations ---------------------------------------------------

export const OVERLAY_ROOT = "fixtures/knowledge-overlays/";

/** The extra knowledge folders a run indexed (versions.knowledge_overlays, red-team runs only). */
export function knowledgeOverlays(versions: Record<string, unknown> | null | undefined): string[] {
  return arr(record(versions).knowledge_overlays).filter((v): v is string => typeof v === "string" && v.trim() !== "");
}

/**
 * A chunk from an overlay folder: under fixtures/knowledge-overlays/, or in a folder the run
 * lists (repo-relative, so a top-level folder is one segment: "redteam-kb"). ingest reads only
 * the files directly inside a folder, so a one-segment folder matches its own files only, and
 * listing a broad folder such as "fixtures" never marks fixtures/knowledge/.
 */
export function isOverlayPath(docPath: string | null | undefined, overlays: readonly string[] = []): boolean {
  if (!docPath) return false;
  const path = docPath.replace(/^\.\//, "");
  if (path.startsWith(OVERLAY_ROOT)) return true;
  const parent = path.slice(0, Math.max(0, path.lastIndexOf("/")));
  return overlays.some((folder) => {
    const prefix = folder.replace(/^\.\//, "").replace(/\/+$/, "");
    return !!prefix && (parent === prefix || (prefix.includes("/") && path.startsWith(`${prefix}/`)));
  });
}

/** Chunk IDs whose document is overlay content (receipts name chunks, not paths). */
export function overlayChunkIds(
  searches: readonly Search[],
  docs: readonly { chunk: { chunk_id: string; doc_path: string } }[],
  overlays: readonly string[],
): Set<string> {
  const ids = new Set<string>();
  for (const s of searches) for (const r of s.results) if (r.chunkId && isOverlayPath(r.docPath, overlays)) ids.add(r.chunkId);
  for (const d of docs) if (isOverlayPath(d.chunk.doc_path, overlays)) ids.add(d.chunk.chunk_id);
  return ids;
}

/**
 * Which agents had each runbook chunk in their context packs (AgentFinding.doc_refs); null
 * before M3 (the API serves the missing lists as []; an M3 finding always records an outcome).
 * An agent that timed out or crashed has only the orchestrator's stand-in finding, whose
 * doc_refs are a default: its context packs in the trace (`events`) say what it saw.
 */
export function seenBy(findings: readonly AgentFinding[], events: readonly TraceEvent[] = []): Map<string, string[]> | null {
  if (!findings.some((f) => f.outcome || f.doc_refs?.length)) return null;
  const packed = packedChunks(events);
  const seen = new Map<string, string[]>();
  for (const f of findings) {
    const standIn = f.outcome === "error" || (!!f.error && !f.doc_refs?.length);
    const refs = standIn ? (packed.get(`${f.agent_role}#${num(f.round) ?? 1}`) ?? []) : (f.doc_refs ?? []);
    for (const id of refs) {
      const agents = seen.get(id) ?? [];
      if (!agents.includes(f.agent_role)) agents.push(f.agent_role);
      seen.set(id, agents);
    }
  }
  return seen;
}

/** Runbook chunk IDs in each agent's context packs, per round ("role#round"); a chunk ID is "stem#heading-n". */
function packedChunks(events: readonly TraceEvent[]): Map<string, string[]> {
  const packed = new Map<string, string[]>();
  let round = 0;
  for (const e of [...events].sort((a, b) => a.seq - b.seq)) {
    if (e.category === "routing.decision") round = num(e.payload.round) ?? round + 1;
    if (e.category !== "context.pack" || !e.agent || !round) continue;
    const key = `${e.agent}#${round}`;
    const ids = packed.get(key) ?? [];
    for (const item of arr(e.payload.items)) {
      if (typeof item === "string" && item.includes("#") && !ids.includes(item)) ids.push(item);
    }
    packed.set(key, ids);
  }
  return packed;
}
