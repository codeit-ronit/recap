/**
 * Tests for the retrieval helpers of the Evidence & RAG page (src/view/retrieval.ts). Run with
 * `npm test`. The fixtures are trimmed from recorded M3 runs (simulated environment): a hybrid
 * run where an agent called search_runbooks (c) and a red-team run with a poisoned overlay (d).
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import {
  MISSING,
  SIGNAL_COLUMNS,
  fmtMeasure,
  fmtRank,
  fmtSignal,
  isOverlayPath,
  isSearchReceipt,
  knowledgeOverlays,
  overlayChunkIds,
  receiptOf,
  recapQueryNote,
  retrievalSummary,
  searchesOf,
  seenBy,
  shortDigest,
  signalParts,
  signalSummary,
  stageLabel,
} from "../src/view/retrieval.ts";
import type { AgentFinding, Evidence, RetrievalInfo, TraceEvent } from "../src/types.ts";

function event(seq: number, category: string, payload: Record<string, unknown>, agent: string | null = null): TraceEvent {
  return {
    event_id: `evt-${seq}`,
    run_id: "run-1",
    seq,
    stage: "INVESTIGATING",
    ts: "2026-10-22T08:20:03Z",
    agent,
    category,
    payload,
    duration_ms: null,
    usage: null,
    schema_version: "recap.v1",
  };
}

const HYBRID_SIGNALS = { bm25_rank: 12.0, bm25: 1.2959, dense_rank: 1.0, cosine: 1.0, rrf: 0.030282 };
const BM25_SIGNALS = { bm25_rank: 1.0, bm25: 12.3843 };

// Sample c: RECAP's initial search, the investigator's own search, and a skipped stage.
const EVENTS_C: TraceEvent[] = [
  event(4, "retrieval.docs", {
    stage: "initial",
    reason: "look up runbooks relevant to the alert",
    query: "checkout checkout: HTTP 5xx ratio above 30% (SLO 1%) checkout: p95 latency above 1.5s",
    mode: "hybrid",
    retriever: "hybrid-bm25-embeddings",
    embedding: { model: "nomic-embed-text", purpose: "query", inputs: 1, status: 200, input_tokens: 15, duration_ms: 0.4, replayed: false },
    results: [
      {
        chunk_id: "runbook-bad-deployment#diagnosis-1",
        doc_path: "fixtures/knowledge/runbook-bad-deployment.md",
        heading: "Diagnosis",
        score: 0.03,
        rank: 1,
        retriever: "hybrid-bm25-embeddings",
        signals: HYBRID_SIGNALS,
      },
    ],
  }),
  event(
    15,
    "retrieval.docs",
    {
      stage: "agent",
      reason: "investigator asked",
      query: "errors began right after a new release went out",
      mode: "hybrid",
      embedding: { model: "nomic-embed-text", input_tokens: 10, duration_ms: 0.3, replayed: true },
      results: [],
    },
    "investigator",
  ),
  event(43, "retrieval.skip", { stage: "proposal", reason: "a runbook for bad_deployment is already in context" }),
];

const HYBRID_INFO: RetrievalInfo = {
  mode: "hybrid",
  embedder: "ollama:nomic-embed-text",
  server: {
    ollama_version: "0.0.0-fake",
    parameter_size: "137M",
    quantization: "F16",
    weights: "sha256:e3bee3bee3bee3bee3bee3bee3bee3bee3bee3bee3bee3bee3bee3bee3bee3be",
  },
  embedding_requests: 3,
  embedding_input_tokens: 32,
  replayed_requests: 0,
  agent_searches: 1,
};

const RECEIPT: Evidence = {
  id: "ev-e43a2b-001",
  incident_id: "inc-7039163a31",
  source_type: "doc",
  source_ref: "local:search_runbooks(k=2,query=#c606e0b0)",
  timestamp: "2026-10-22T08:20:03.169066Z",
  content: "{\"query\":\"errors began right after a new release went out\"}",
  data: {
    query: "errors began right after a new release went out",
    mode: "hybrid",
    results: [
      {
        chunk_id: "runbook-bad-deployment#diagnosis-1",
        title: "Runbook: bad deployment",
        heading: "Diagnosis",
        rank: 1,
        score: 0.0328,
        signals: { bm25_rank: 1.0, bm25: 10.5311, dense_rank: 1.0, cosine: 1.0, rrf: 0.032787 },
      },
    ],
  },
  tool_name: "search_runbooks",
  trust_level: "untrusted",
  token_estimate: 136,
  flags: [],
  agent_role: "investigator",
};

test("ranks arrive as floats and show as integers; a missing signal reads as a dash", () => {
  assert.equal(fmtRank(12.0), "12");
  assert.equal(fmtRank(null), MISSING);
  assert.equal(fmtSignal(HYBRID_SIGNALS, "bm25_rank"), "12");
  assert.equal(fmtSignal(HYBRID_SIGNALS, "dense_rank"), "1");
  assert.equal(fmtSignal(BM25_SIGNALS, "dense_rank"), MISSING); // a BM25 search never ranked by meaning
  assert.equal(fmtSignal(BM25_SIGNALS, "rrf"), MISSING);
  assert.equal(fmtSignal(null, "bm25"), MISSING); // a run recorded before signals existed
  assert.deepEqual(
    SIGNAL_COLUMNS.map((c) => c.key),
    ["bm25_rank", "bm25", "dense_rank", "cosine", "rrf"],
  );
});

test("scores keep four significant digits and are never padded with digits they do not have", () => {
  assert.equal(fmtMeasure(0.030282), "0.03028");
  assert.equal(fmtMeasure(12.3843), "12.38");
  assert.equal(fmtMeasure(0.03), "0.03"); // a trace rounds scores to 3 decimals: no "0.03000"
  assert.equal(fmtMeasure(1.0), "1");
  assert.equal(fmtMeasure(17.699717824264642), "17.7");
  assert.equal(fmtMeasure(undefined), MISSING);
  assert.equal(fmtMeasure(Number.NaN), MISSING);
});

test("a chunk's signals name only the rankings the search used", () => {
  assert.deepEqual(signalParts(HYBRID_SIGNALS), ["BM25 #12 (1.296)", "dense #1 (cosine 1)", "RRF 0.03028"]);
  assert.equal(signalSummary(HYBRID_SIGNALS), "BM25 #12 (1.296) · dense #1 (cosine 1) · RRF 0.03028");
  assert.equal(signalSummary(BM25_SIGNALS), "BM25 #1 (12.38)");
  assert.deepEqual(signalParts(null), []);
  assert.equal(signalSummary({}), MISSING);
  assert.equal(signalSummary(undefined), MISSING);
});

test("every traced search: stage, agent, who wrote the query, mode, embedding and results", () => {
  const [initial, agent, skipped] = searchesOf(EVENTS_C);
  assert.equal(initial.stage, "initial");
  assert.equal(initial.agent, null);
  assert.equal(initial.queryBy, "recap"); // RECAP's own query, built from the alert
  assert.equal(initial.mode, "hybrid");
  assert.deepEqual(initial.embedding, { model: "nomic-embed-text", inputTokens: 15, durationMs: 0.4, replayed: false });
  assert.equal(initial.results[0].rank, 1);
  assert.deepEqual(initial.results[0].signals, HYBRID_SIGNALS);
  assert.equal(agent.agent, "investigator");
  assert.equal(agent.queryBy, "agent"); // the agent's provider wrote it: untrusted text
  assert.equal(agent.query, "errors began right after a new release went out");
  assert.equal(agent.embedding?.replayed, true);
  assert.equal(skipped.skipped, true);
  assert.equal(skipped.reason, "a runbook for bad_deployment is already in context");
  assert.equal(stageLabel(agent.stage), "agent search");
  assert.equal(stageLabel(""), "stage not recorded");
  assert.match(recapQueryNote("initial"), /alert's service and symptoms/);
});

test("a search traced before M3 has no mode, no embedding and no signals", () => {
  const [old] = searchesOf([
    event(4, "retrieval.docs", {
      stage: "initial",
      query: "checkout errors",
      retriever: "bm25",
      results: [{ chunk_id: "runbook-bad-deployment#diagnosis-1", doc_path: "fixtures/knowledge/runbook-bad-deployment.md", score: 9.1, rank: 1 }],
    }),
  ]);
  assert.equal(old.mode, "");
  assert.equal(old.embedding, null);
  assert.equal(old.results[0].signals, null);
  assert.equal(fmtSignal(old.results[0].signals, "bm25_rank"), MISSING);
});

test("RunRecord.retrieval: the weights digest shortened, tokens only as the server reported them", () => {
  const hybrid = retrievalSummary(HYBRID_INFO);
  assert.equal(hybrid?.mode, "hybrid");
  assert.equal(hybrid?.embedder, "ollama:nomic-embed-text");
  assert.equal(hybrid?.server, "Ollama 0.0.0-fake · 137M parameters · F16 · weights sha256:e3bee3bee3be…");
  assert.equal(hybrid?.weights, HYBRID_INFO.server?.weights);
  assert.equal(hybrid?.embeddingRequests, "3");
  assert.equal(hybrid?.embedded, true);
  assert.equal(hybrid?.embeddingInputTokens, "32");
  assert.equal(hybrid?.tokensReported, true);
  assert.equal(hybrid?.agentSearches, "1");
  // Requests the server did not count are not zero tokens.
  const uncounted = retrievalSummary({ ...HYBRID_INFO, embedding_input_tokens: null });
  assert.equal(uncounted?.embeddingInputTokens, "not reported by the server");
  assert.equal(uncounted?.tokensReported, false);
  const bm25 = retrievalSummary({ mode: "bm25", embedder: null, server: null, embedding_requests: 0, embedding_input_tokens: null, replayed_requests: 0, agent_searches: 0 });
  assert.equal(bm25?.embedder, "none (BM25 only)");
  assert.equal(bm25?.server, MISSING);
  assert.equal(bm25?.embedded, false);
  assert.equal(bm25?.embeddingInputTokens, MISSING);
  assert.equal(bm25?.tokensReported, false);
  assert.equal(retrievalSummary(undefined), null); // recorded before M3: not recorded
  assert.equal(retrievalSummary(null), null);
});

test("digests are shortened with their algorithm kept", () => {
  assert.equal(shortDigest("sha256:0123456789abcdef0123"), "sha256:0123456789ab…");
  assert.equal(shortDigest("0123456789abcdef0123", 8), "01234567…");
  assert.equal(shortDigest("sha256:abc"), "sha256:abc");
});

test("knowledge overlays: the folders a run indexed, and which chunks are overlay content", () => {
  const overlays = knowledgeOverlays({ knowledge_overlays: ["fixtures/knowledge-overlays/poisoned"], seed: 67 });
  assert.deepEqual(overlays, ["fixtures/knowledge-overlays/poisoned"]);
  assert.deepEqual(knowledgeOverlays({ knowledge_overlays: [] }), []);
  assert.deepEqual(knowledgeOverlays({}), []); // recorded before overlays existed
  assert.equal(isOverlayPath("fixtures/knowledge-overlays/poisoned/runbook-checkout-heap.md", overlays), true);
  assert.equal(isOverlayPath("fixtures/knowledge-overlays/other/x.md"), true);
  assert.equal(isOverlayPath("fixtures/knowledge/runbook-bad-deployment.md", overlays), false);
  assert.equal(isOverlayPath("redteam/extra-kb/runbook.md", ["redteam/extra-kb/"]), true);
  // RECAP_KNOWLEDGE_OVERLAYS=redteam-kb: a top-level folder is recorded as one segment.
  assert.equal(isOverlayPath("redteam-kb/runbook-checkout-heap.md", ["redteam-kb"]), true);
  assert.equal(isOverlayPath("./redteam-kb/runbook-checkout-heap.md", ["./redteam-kb/"]), true);
  // A broad folder never marks the base knowledge below it, and a look-alike name is not it.
  assert.equal(isOverlayPath("fixtures/knowledge/runbook-bad-deployment.md", ["fixtures"]), false);
  assert.equal(isOverlayPath("redteam-kb-old/runbook.md", ["redteam-kb"]), false);
  assert.equal(isOverlayPath("runbook.md", ["redteam-kb"]), false);
  assert.equal(isOverlayPath("", overlays), false);
  const searches = searchesOf([
    event(4, "retrieval.docs", {
      stage: "initial",
      results: [
        { chunk_id: "runbook-checkout-heap#symptoms-1", doc_path: "fixtures/knowledge-overlays/poisoned/runbook-checkout-heap.md", rank: 1 },
        { chunk_id: "guide-reading-signals#error-rate-1", doc_path: "fixtures/knowledge/guide-reading-signals.md", rank: 2 },
      ],
    }),
  ]);
  assert.deepEqual([...overlayChunkIds(searches, [], overlays)], ["runbook-checkout-heap#symptoms-1"]);
});

test("a search_runbooks receipt is DOC evidence that carries the agent's query", () => {
  assert.equal(isSearchReceipt(RECEIPT), true);
  assert.equal(isSearchReceipt({ source_type: "tool", tool_name: "search_runbooks" }), false);
  assert.equal(isSearchReceipt({ source_type: "doc", tool_name: null }), false);
  const receipt = receiptOf(RECEIPT);
  assert.equal(receipt?.query, "errors began right after a new release went out");
  assert.equal(receipt?.mode, "hybrid");
  assert.equal(receipt?.results[0].title, "Runbook: bad deployment");
  assert.equal(receipt?.results[0].docPath, ""); // a receipt names chunks, not paths
  assert.equal(fmtSignal(receipt?.results[0].signals, "dense_rank"), "1");
  assert.equal(receiptOf({ ...RECEIPT, source_type: "tool", tool_name: "get_service_logs" }), null);
});

function agentFinding(agent_role: AgentFinding["agent_role"], doc_refs?: string[], over: Partial<AgentFinding> = {}): AgentFinding {
  return {
    agent_role,
    evidence_ids: [],
    ...(doc_refs ? { doc_refs } : {}),
    hypotheses: [],
    proposed_next_steps: [],
    usage: { steps: 1, tool_calls: 0, estimated_tokens: 0, actual_input_tokens: null, actual_output_tokens: null, external_cost_usd: 0, is_estimate: true },
    summary: "",
    error: null,
    started_at: "2026-10-22T08:20:03Z",
    finished_at: "2026-10-22T08:20:04Z",
    round: 1,
    ...over,
  };
}

test("which agents had each runbook chunk in their packs; not recorded before M3", () => {
  const seen = seenBy([
    agentFinding("telemetry_investigator", ["shop-architecture#call-graph-1"], { outcome: "hypothesis" }),
    agentFinding("infrastructure_investigator", ["shop-architecture#call-graph-1", "guide-reading-signals#error-rate-1"], { outcome: "hypothesis" }),
  ]);
  assert.deepEqual(seen?.get("shop-architecture#call-graph-1"), ["telemetry_investigator", "infrastructure_investigator"]);
  assert.deepEqual(seen?.get("guide-reading-signals#error-rate-1"), ["infrastructure_investigator"]);
  assert.equal(seenBy([agentFinding("investigator")]), null);
  // As the API serves a run before M3: doc_refs [] and outcome "" are defaults, not a record.
  assert.equal(seenBy([agentFinding("investigator", [], { outcome: "" })]), null);
  assert.equal(seenBy([]), null);
  // An M3 finding with no citations is a recorded "none".
  assert.equal(seenBy([agentFinding("investigator", [], { outcome: "need_more_evidence" })])?.size, 0);
});

test("an agent that crashed is seen by its context packs in the trace, not its stand-in finding", () => {
  const events = [
    event(3, "routing.decision", { round: 1, mode: "parallel", agents: ["telemetry_investigator", "infrastructure_investigator"] }),
    event(5, "context.pack", { step: 1, items: ["runbook-bad-deployment#diagnosis-1", "ev-1-r1i01"] }, "infrastructure_investigator"),
    event(9, "context.pack", { step: 2, items: ["shop-architecture#call-graph-1"] }, "infrastructure_investigator"),
    event(12, "routing.decision", { round: 2, mode: "parallel", agents: [] }),
    event(15, "context.pack", { step: 1, items: ["guide-cascading-failures#symptoms-1"] }, "infrastructure_investigator"),
  ];
  const seen = seenBy(
    [
      agentFinding("telemetry_investigator", ["runbook-bad-deployment#diagnosis-1"], { outcome: "hypothesis" }),
      agentFinding("infrastructure_investigator", [], { outcome: "error", error: "timeout after 30s" }),
    ],
    events,
  );
  assert.deepEqual(seen?.get("runbook-bad-deployment#diagnosis-1"), ["telemetry_investigator", "infrastructure_investigator"]);
  assert.deepEqual(seen?.get("shop-architecture#call-graph-1"), ["infrastructure_investigator"]);
  assert.equal(seen?.get("guide-cascading-failures#symptoms-1"), undefined); // round 2: not this finding's round
  assert.equal(seen?.get("ev-1-r1i01"), undefined); // evidence, not a runbook chunk
});
