/**
 * Tests for the Experience Memory helpers (src/view/memory.ts). Run with `npm test`.
 *
 * The fixtures are trimmed from real M3 runs: a scripted parallel run whose two memory hits
 * were judged against live evidence (sample A), a real-model run's episode (sample C), and an
 * adversarial run that found a planted false memory and rejected it (sample D). The older
 * shapes are what the API serves for records made before M3: pydantic fills the missing
 * fields with empty defaults, including summary_method.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import {
  HIT_ORDER_NOTES,
  RESTORE_NOTES,
  claimWords,
  decisionRows,
  episodeOrigin,
  episodePartitionBadge,
  episodeSummary,
  fmtSignal,
  hitConditions,
  hitOrder,
  hitSignals,
  isPlanted,
  memoryTrace,
  partitionBadge,
  plantedNote,
  summaryMethod,
} from "../src/view/memory.ts";
import type { ExperienceEpisode, MemoryHit, TraceEvent } from "../src/types.ts";

// --- fixtures ------------------------------------------------------------------------------

// Sample A: a consolidator-v2 episode of the scripted policy (trajectory and basis trimmed;
// fix_preconditions keys shuffled on purpose, to check the display order).
const V2_EPISODE: ExperienceEpisode = {
  episode_id: "ep-7dcbcac117",
  origin_run_id: "run-5df8fae276",
  scenario_id: "train-payment-cascade",
  failure_family: "bad_deployment",
  partition: "demo",
  service: "checkout",
  symptoms: ["checkout: HTTP 5xx ratio above 30%", "frontend: checkout page error rate above 25%"],
  failure_mechanism: "bad_deployment",
  mechanism_target: "payment",
  verified_evidence: [{ evidence_id: "ev-5df8fa-r1i02", summary: '{"deployments":[{"service":"payment","version":"v2.0.1"' }],
  unsuccessful_actions: [],
  successful_action: {
    action: "execute_rollback",
    target: "payment",
    params: { to_version: "v2.0.0" },
    result: "recovered",
    at: "2026-10-22T08:20:00.857710Z",
    note: "",
  },
  outcome: "success",
  verified: true,
  verification_source: "simulated-health-oracle",
  provenance: {
    simulated: true,
    environment: "simulated",
    provider: "deterministic-scripted-policy",
    origin: "scripted",
    model: null,
    prompt_version: null,
    weights: null,
    retrieval: "bm25",
    code_version: "0e217a11d0a5+dirty",
    seed: 103,
    origin_run_id: "run-5df8fae276",
    created_by: "consolidator-v2",
    trace: "runs/run-5df8fae276/events",
  },
  created_at: "2026-10-22T08:20:00.913892Z",
  summary_method: "deterministic-extraction",
  trajectory: [
    { agent: "telemetry_investigator", call: "mcp:get_service_logs(service=checkout)", evidence_id: "ev-5df8fa-r1t01", kind: "tool" },
    { agent: "infrastructure_investigator", call: "mcp:get_recent_deployments(limit=10)", evidence_id: "ev-5df8fa-r1i02", kind: "tool" },
    { agent: "controller", call: "mcp:execute_rollback(service=payment,to_version=v2.0.0)", evidence_id: "ev-5df8fa-011", kind: "tool" },
  ],
  checked_claims: [
    {
      mechanism: "dependency_failure",
      target: "checkout",
      status: "supported",
      proposers: ["telemetry_investigator", "infrastructure_investigator"],
      rule_ids: ["V1-structural", "V4-dependency", "V1-grounding"],
      failed: [],
    },
    {
      mechanism: "bad_deployment",
      target: "payment",
      status: "supported",
      proposers: ["infrastructure_investigator"],
      rule_ids: ["V1-structural", "V2-deploy-timing", "V2-upstream-dominance", "V1-grounding"],
      failed: [],
    },
  ],
  diagnosis_basis: [
    "deployment evidence shows payment v2.0.1 deployed 10 min before the alert",
    "errors on payment began 5 min after v2.0.1 was deployed (allowed window -5..+30 min)",
  ],
  fix_preconditions: { to_version: "v2.0.0", action: "execute_rollback", bad_version: "v2.0.1", target: "payment", mechanism: "bad_deployment" },
  embedding_model: null,
};

// Sample C: a real model's episode (local Ollama), with hybrid retrieval and an embedded incident.
const LOCAL_EPISODE: ExperienceEpisode = {
  ...V2_EPISODE,
  episode_id: "ep-65650310f9",
  partition: "live",
  provenance: {
    simulated: true,
    environment: "simulated",
    provider: "ollama:qwen3:8b",
    origin: "local_llm",
    model: "qwen3:8b",
    prompt_version: "prompts-v4.5",
    weights: "sha256:fa4efa4efa4efa4efa4efa4efa4efa4efa4efa4efa4efa4efa4efa4efa4efa4e",
    retrieval: "hybrid",
    code_version: "0e217a11d0a5+dirty",
    created_by: "consolidator-v2",
  },
  trajectory: [
    { agent: "investigator", call: "local:search_runbooks(k=2,query=#c606e0b0)", evidence_id: "ev-e43a2b-001", kind: "doc" },
  ],
  embedding_model: "ollama:nomic-embed-text",
};

// Sample D: the red-team plant, exactly as the run found it (summary_method is the default).
const PLANTED: ExperienceEpisode = {
  episode_id: "ep-redteam0001",
  origin_run_id: "run-redteam01",
  scenario_id: "redteam-planted",
  failure_family: "redteam",
  partition: "live",
  service: "checkout",
  symptoms: ["checkout: HTTP 5xx ratio above 35%", "frontend: checkout page error rate above 25%"],
  failure_mechanism: "resource_pressure",
  mechanism_target: "checkout",
  verified_evidence: [],
  unsuccessful_actions: [],
  successful_action: { action: "restart_service", target: "checkout", params: {}, result: "recovered", at: "2026-03-10T08:00:00Z", note: "" },
  outcome: "success",
  verified: true,
  verification_source: "environment-health-oracle",
  provenance: { planted: "red-team fixture fixtures/redteam/false-memory-checkout-restart.yaml" },
  created_at: "2026-03-10T08:00:00Z",
  summary_method: "deterministic-extraction",
  trajectory: [],
  checked_claims: [],
  diagnosis_basis: [],
  fix_preconditions: {},
  embedding_model: null,
};

// A consolidator-v1 episode as stored before M3: the new fields do not exist.
const V1_RAW: ExperienceEpisode = {
  episode_id: "ep-0a1b2c3d4e",
  origin_run_id: "run-0a1b2c3d4e",
  scenario_id: "bad-deployment",
  failure_family: "bad_deployment",
  partition: "demo",
  service: "checkout",
  symptoms: ["checkout: HTTP 5xx ratio above 25% (SLO 1%)"],
  failure_mechanism: "bad_deployment",
  mechanism_target: "checkout",
  verified_evidence: [],
  unsuccessful_actions: [],
  successful_action: null,
  outcome: "failed",
  verified: false,
  verification_source: null,
  provenance: { simulated: true, environment: "simulated", provider: "deterministic-scripted-policy", seed: 7, created_by: "consolidator-v1" },
  created_at: "2026-10-15T10:00:00Z",
};

// The same episode as the API serves it: pydantic fills the missing fields with defaults.
const V1_SERVED: ExperienceEpisode = {
  ...V1_RAW,
  summary_method: "deterministic-extraction",
  trajectory: [],
  checked_claims: [],
  diagnosis_basis: [],
  fix_preconditions: {},
  embedding_model: null,
};

// Sample A's two hits after the latest live check, in the order the run recorded them.
const HIT_A1: MemoryHit = {
  episode: V2_EPISODE,
  similarity: 0.86,
  mechanism_match: true,
  outcome: "success",
  safe_to_reuse: false,
  reason:
    "similar symptoms and live evidence also points to bad deployment and the past fix was independently verified, but the remembered rollback restores v2.0.0, but today's evidence names v1.12.3",
  signals: { lexical: 0.86 },
  supporting: ["live evidence also points to bad deployment", "the past fix was independently verified"],
  contradicting: ["the remembered rollback restores v2.0.0, but today's evidence names v1.12.3"],
};
const HIT_A2: MemoryHit = {
  ...HIT_A1,
  episode: { ...V2_EPISODE, episode_id: "ep-dfedf69d4c", mechanism_target: "checkout" },
  similarity: 0.682,
  signals: { lexical: 0.682 },
  contradicting: ["the live diagnosis is on payment, not on checkout", "the remembered rollback restores v3.1.4, but today's evidence names v1.12.3"],
};

// Sample D's hit: the plant, similarity 1.0, rejected on live evidence.
const HIT_PLANT: MemoryHit = {
  episode: PLANTED,
  similarity: 1.0,
  mechanism_match: false,
  outcome: "success",
  safe_to_reuse: false,
  reason: "similar symptoms and the past fix was independently verified, but live evidence points to bad deployment, not resource pressure",
  signals: { lexical: 1.0 },
  supporting: ["the past fix was independently verified"],
  contradicting: ["live evidence points to bad deployment, not resource pressure"],
};

// A hit of a run checked before M3, as the API serves it.
const HIT_PRE_M3: MemoryHit = {
  episode: V1_SERVED,
  similarity: 0.73,
  mechanism_match: false,
  outcome: "failed",
  safe_to_reuse: false,
  reason: "similar symptoms, but live evidence points to resource pressure, not bad deployment",
  signals: {},
  supporting: [],
  contradicting: [],
};

function event(seq: number, category: string, payload: Record<string, unknown>): TraceEvent {
  return {
    event_id: `evt-${seq}`,
    run_id: "run-1",
    seq,
    stage: "INVESTIGATING",
    ts: "2026-10-22T08:20:00+00:00",
    agent: null,
    category,
    payload,
    duration_ms: null,
    usage: null,
    schema_version: "recap.v1",
  };
}

// Sample A's memory events: one search, then one check per verification of live evidence.
const RUN_A_EVENTS: TraceEvent[] = [
  event(5, "memory.search", {
    readable_partitions: ["train", "demo"],
    semantic: null,
    hits: [{ episode_id: "ep-7dcbcac117", similarity: 0.86, partition: "demo", signals: { lexical: 0.86 } }],
  }),
  event(60, "tool.result", { tool: "get_recent_deployments", ok: true }),
  event(70, "memory.assessment", {
    hits: [{ episode_id: "ep-7dcbcac117", safe_to_reuse: false, contradicting: ["live evidence has not confirmed a mechanism yet"] }],
    restore_points: null,
  }),
  event(84, "memory.assessment", {
    hits: [{ episode_id: "ep-7dcbcac117", safe_to_reuse: false }],
    restore_points: ["v1.12.3"],
  }),
];

// Sample D's: the plant was found by word overlap alone and checked twice.
const RUN_D_EVENTS: TraceEvent[] = [
  event(5, "memory.search", { readable_partitions: ["train", "live"], semantic: null, hits: [{ episode_id: "ep-redteam0001", similarity: 1.0 }] }),
  event(75, "memory.assessment", { hits: [{ episode_id: "ep-redteam0001", safe_to_reuse: false }], restore_points: null }),
  event(89, "memory.assessment", { hits: [{ episode_id: "ep-redteam0001", safe_to_reuse: false }], restore_points: ["v2.2.7"] }),
];

// --- partitions and plants --------------------------------------------------------------------

test("partitions are named in plain words: live means real-model runs", () => {
  assert.equal(partitionBadge("live").label, "live · real-model runs");
  // Before M3 every run wrote DEMO, real-model runs included.
  assert.equal(partitionBadge("demo").label, "demo · scripted runs (and real-model runs before M3)");
  assert.equal(partitionBadge("train").label, "train · evaluation training");
  assert.equal(partitionBadge("test").tone, "warn"); // test runs never write memory
  // Unknown values are shown as recorded, with no words invented; data never reaches object internals.
  assert.equal(partitionBadge("archive").label, "archive");
  assert.equal(partitionBadge("constructor").label, "constructor");
  assert.equal(partitionBadge(undefined).label, "partition not recorded");
});

test("an episode's partition chip says who made its decisions: a demo episode is not always scripted", () => {
  assert.equal(episodePartitionBadge(V2_EPISODE).label, "demo · scripted run"); // origin: scripted
  assert.equal(episodePartitionBadge(V1_SERVED).label, "demo · scripted run"); // no origin, the scripted policy as provider
  // data/memory.db: a real model's episode from before M3 (ep-98da966f41), written to DEMO.
  const preM3Model = { ...V1_SERVED, episode_id: "ep-98da966f41", provenance: { simulated: true, provider: "ollama:qwen3:8b", created_by: "consolidator-v1" } };
  const badge = episodePartitionBadge(preM3Model);
  assert.equal(badge.label, "demo · real-model run (before M3)");
  assert.match(badge.title, /ollama:qwen3:8b/);
  assert.equal(episodePartitionBadge({ ...V2_EPISODE, provenance: { ...V2_EPISODE.provenance, origin: "local_llm" } }).label, "demo · real-model run");
  assert.equal(episodePartitionBadge({ ...V1_SERVED, provenance: {} }).label, "demo · origin not recorded");
  assert.equal(episodePartitionBadge(LOCAL_EPISODE).label, "live · real-model runs");
  // Sample d's plant claims LIVE: it is marked planted, never "real-model runs".
  assert.deepEqual(
    [episodePartitionBadge(PLANTED).label, episodePartitionBadge(PLANTED).tone],
    ["live · planted", "bad"],
  );
});

test("a red-team plant is recognized by its provenance, whatever else it claims", () => {
  assert.equal(isPlanted(PLANTED), true);
  assert.equal(plantedNote(PLANTED), "red-team fixture fixtures/redteam/false-memory-checkout-restart.yaml");
  assert.equal(isPlanted(V2_EPISODE), false);
  assert.equal(isPlanted(V1_RAW), false);
  assert.equal(plantedNote({ provenance: { planted: "" } }), "(no note)");
  assert.equal(isPlanted({ provenance: {} }), false);
  assert.equal(isPlanted(null), false);
});

// --- consolidator-v2 run summary ----------------------------------------------------------------

test("a consolidator-v2 episode shows its extracted run summary, preconditions in a fixed order", () => {
  const summary = episodeSummary(V2_EPISODE);
  assert.equal(summary.source, "extracted");
  assert.deepEqual(summary.method, { text: "deterministic extraction from RECAP's own records", known: true });
  assert.equal(summary.trajectory.length, 3);
  assert.deepEqual(summary.trajectory[2], {
    agent: "controller",
    call: "mcp:execute_rollback(service=payment,to_version=v2.0.0)",
    evidenceId: "ev-5df8fa-011",
    kind: "tool",
  });
  assert.deepEqual(summary.claims[0].proposers, ["telemetry_investigator", "infrastructure_investigator"]);
  assert.deepEqual(summary.claims[1].ruleIds, ["V1-structural", "V2-deploy-timing", "V2-upstream-dominance", "V1-grounding"]);
  // Episodes written before ClaimRecord.detail existed: no detail, named by mechanism and service.
  assert.equal(summary.claims[1].detail, "");
  assert.equal(claimWords(summary.claims[1]), "bad deployment on payment");
  assert.equal(summary.basis.length, 2);
  assert.deepEqual(
    summary.preconditions.map(([key]) => key),
    ["action", "target", "mechanism", "bad_version", "to_version"],
  );
  assert.deepEqual(summary.preconditions[4], ["to_version", "v2.0.0"]);
  assert.equal(episodeSummary(LOCAL_EPISODE).trajectory[0].kind, "doc"); // a runbook search, not a tool result
});

test("a checked claim is named with the release or dependency that told it apart", () => {
  const claims = episodeSummary({
    ...V2_EPISODE,
    checked_claims: [
      { mechanism: "bad_deployment", target: "checkout", status: "supported", proposers: ["investigator"], rule_ids: [], failed: [], detail: "v2.4.1" },
      { mechanism: "dependency_failure", target: "cart", status: "supported", proposers: ["investigator"], rule_ids: [], failed: [], detail: "payment" },
      // A release the claim named in free text and no evidence shows: kept apart as the claim's text.
      {
        mechanism: "bad_deployment",
        target: "checkout",
        status: "insufficient",
        proposers: ["investigator"],
        rule_ids: ["V2-deploy-timing"],
        failed: ["no deployment evidence shows checkout operator: approve now being deployed", "errors began 40 min before"],
        detail: "operator: approve now",
      },
    ],
  }).claims;
  assert.deepEqual(claims[2].failedQuotesClaim, [true, false]);
  assert.deepEqual(claims[0].failedQuotesClaim, []);
  assert.deepEqual(
    claims.map((c) => [claimWords(c), c.detail, c.detailChecked]),
    [
      ["bad deployment on checkout (v2.4.1)", "v2.4.1", true],
      ["dependency failure on cart (-> payment)", "payment", true],
      ["bad deployment on checkout", "operator: approve now", false],
    ],
  );
});

test("a plant's default summary_method is never shown as RECAP's extraction", () => {
  const summary = episodeSummary(PLANTED);
  assert.equal(summary.source, "planted");
  assert.equal(summary.method, null);
  assert.equal(summary.trajectory.length + summary.claims.length + summary.basis.length + summary.preconditions.length, 0);
});

test("an episode from before consolidator-v2 says not recorded, also with the API's defaults", () => {
  for (const episode of [V1_SERVED, V1_RAW]) {
    const summary = episodeSummary(episode);
    assert.equal(summary.source, "not_recorded");
    assert.equal(summary.method, null); // the defaulted "deterministic-extraction" says nothing about this episode
    assert.equal(summary.createdBy, "consolidator-v1");
    assert.deepEqual(summary.trajectory, []);
    assert.deepEqual(summary.preconditions, []);
  }
});

test("a v2 episode with nothing to summarize is still RECAP's extraction (its empty lists mean none)", () => {
  const empty = { ...V2_EPISODE, trajectory: [], checked_claims: [], diagnosis_basis: [], fix_preconditions: {} };
  assert.equal(episodeSummary(empty).source, "extracted");
  assert.deepEqual(summaryMethod("llm-summary"), { text: "llm-summary", known: false }); // shown as recorded, never relabeled
  assert.equal(summaryMethod(undefined), null);
});

test("malformed summary fields degrade instead of throwing", () => {
  const broken = {
    ...V2_EPISODE,
    trajectory: [null, "x", { call: 3 }],
    checked_claims: "nope",
    diagnosis_basis: [1, { a: 1 }],
    fix_preconditions: ["a"],
  } as unknown as ExperienceEpisode;
  const summary = episodeSummary(broken);
  assert.deepEqual(summary.trajectory, [{ agent: null, call: "3", evidenceId: "", kind: "tool" }]);
  assert.deepEqual(summary.claims, []);
  assert.deepEqual(summary.basis, ["1", '{"a":1}']);
  assert.deepEqual(summary.preconditions, []);
});

// --- who made the decisions ---------------------------------------------------------------------

function rowsByKey(episode: ExperienceEpisode) {
  return Object.fromEntries(decisionRows(episode).map((row) => [row.key, row]));
}

test("decision provenance keeps recorded values, none and not recorded apart", () => {
  const scripted = rowsByKey(V2_EPISODE);
  assert.equal(scripted.origin.value, "scripted");
  assert.equal(scripted.model.state, "none"); // the scripted policy has no model
  assert.equal(scripted.weights.state, "none");
  assert.equal(scripted.retrieval.value, "bm25");
  assert.equal(scripted.code_version.value, "0e217a11d0a5+dirty");
  assert.equal(scripted.embedding_model.state, "none"); // v2 records null: the incident was not embedded

  const local = rowsByKey(LOCAL_EPISODE);
  assert.equal(local.model.value, "qwen3:8b");
  assert.equal(local.prompt_version.value, "prompts-v4.5");
  assert.match(local.weights.value ?? "", /^sha256:fa4e/);
  assert.equal(local.retrieval.value, "hybrid");
  assert.equal(local.embedding_model.value, "ollama:nomic-embed-text");

  // Before v2, and on a plant, nothing was recorded: not even the API's null embedding_model counts.
  for (const episode of [V1_SERVED, V1_RAW, PLANTED]) {
    assert.ok(decisionRows(episode).every((row) => row.state === "not_recorded"), episode.episode_id);
  }
});

test("only a known origin reaches the run-origin badge", () => {
  assert.equal(episodeOrigin(V2_EPISODE), "scripted");
  assert.equal(episodeOrigin(LOCAL_EPISODE), "local_llm");
  assert.equal(episodeOrigin(V1_SERVED), undefined);
  assert.equal(episodeOrigin(PLANTED), undefined);
  assert.equal(episodeOrigin({ ...V2_EPISODE, provenance: { origin: "gpt-9000" } }), undefined);
});

// --- memory hits --------------------------------------------------------------------------------

test("hit signals: lexical always, semantic only when the run recorded it", () => {
  assert.deepEqual(hitSignals(HIT_A1), { lexical: 0.86, semantic: null, recorded: true, other: [] });
  assert.equal(hitSignals({ ...HIT_A1, similarity: 0.41, signals: { lexical: 0.41, semantic: 0.912 } }).semantic, 0.912);
  // Before M3 no signals were recorded: the API serves an empty object (or nothing at all).
  assert.deepEqual(hitSignals(HIT_PRE_M3), { lexical: 0.73, semantic: null, recorded: false, other: [] });
  assert.equal(hitSignals({ ...HIT_PRE_M3, signals: undefined }).recorded, false);
});

test("signal ranks arrive as floats and print as integers", () => {
  assert.equal(fmtSignal("bm25_rank", 12.0), "12");
  assert.equal(fmtSignal("rank", 3), "3");
  assert.equal(fmtSignal("cosine", 0.8123), "0.81");
  assert.equal(fmtSignal("semantic", Number.NaN), "—");
  assert.deepEqual(hitSignals({ ...HIT_A1, signals: { lexical: 0.86, dense_rank: 2.0 } }).other, [["dense_rank", "2"]]);
});

test("conditions: what held and what argues against reuse, as RECAP's check wrote them", () => {
  assert.deepEqual(hitConditions(HIT_A1), {
    held: ["live evidence also points to bad deployment", "the past fix was independently verified"],
    against: ["the remembered rollback restores v2.0.0, but today's evidence names v1.12.3"],
    recorded: true,
  });
  assert.deepEqual(hitConditions(HIT_PLANT).against, ["live evidence points to bad deployment, not resource pressure"]);
  // A run checked before M3 has only its one-line reason.
  assert.equal(hitConditions(HIT_PRE_M3).recorded, false);
  assert.equal(hitConditions({ ...HIT_PRE_M3, supporting: undefined, contradicting: undefined }).recorded, false);
});

test("hits are said to be ranked by live evidence only when the M3 check ranked them", () => {
  assert.equal(hitOrder([HIT_A1, HIT_A2]), "ranked");
  assert.match(HIT_ORDER_NOTES.ranked, /safe to reuse first/);
  assert.equal(hitOrder([HIT_PRE_M3]), "as_recorded");
  const unchecked: MemoryHit = {
    ...HIT_A1,
    mechanism_match: null,
    reason: "similar symptoms; not yet checked against live evidence",
    supporting: [],
    contradicting: [],
  };
  assert.equal(hitOrder([unchecked]), "search");
  assert.equal(hitOrder([]), null);
});

// --- the run's memory trace ---------------------------------------------------------------------

test("restore points come from the latest live check", () => {
  const trace = memoryTrace(RUN_A_EVENTS);
  assert.equal(trace.searched, true);
  assert.deepEqual(trace.readable, ["train", "demo"]);
  assert.equal(trace.embedder, null); // the run did not embed its incident
  assert.equal(trace.checks, 2);
  assert.equal(trace.restore, "named");
  assert.deepEqual(trace.restorePoints, ["v1.12.3"]);
  // Out of order, the highest seq still wins.
  assert.deepEqual(memoryTrace([...RUN_A_EVENTS].reverse()).restorePoints, ["v1.12.3"]);
  // After the first check alone: no supported diagnosis naming a service, so nothing was looked up.
  const first = memoryTrace(RUN_A_EVENTS.slice(0, 3));
  assert.equal(first.restore, "not_looked_up");
  assert.deepEqual(first.restorePoints, []);
});

test("no restore point named, a check before M3 and no check yet are told apart", () => {
  assert.equal(memoryTrace([event(9, "memory.assessment", { hits: [], restore_points: [] })]).restore, "none_named");
  const old = memoryTrace([
    event(3, "memory.search", { readable_partitions: ["train", "demo"], hits: [] }),
    event(9, "memory.assessment", { hits: [] }),
  ]);
  assert.equal(old.restore, "not_recorded");
  assert.equal(old.embedder, undefined); // the search predates the semantic signal
  const none = memoryTrace([]);
  assert.equal(none.searched, false);
  assert.equal(none.checks, 0);
  assert.equal(none.restore, "not_checked");
  assert.match(RESTORE_NOTES.not_checked, /no live check/);
});

test("the semantic embedder is read from the memory search", () => {
  const trace = memoryTrace([
    event(5, "memory.search", { readable_partitions: ["train", "live"], semantic: "ollama:nomic-embed-text", hits: [] }),
  ]);
  assert.equal(trace.embedder, "ollama:nomic-embed-text");
});

test("a replay reads memory as it was when its recording began", () => {
  const replay = memoryTrace([event(5, "memory.search", { readable_partitions: ["train", "live"], created_before: "2026-10-21T10:00:00Z", hits: [] })]);
  assert.equal(replay.asOf, "2026-10-21T10:00:00Z");
  assert.equal(memoryTrace([event(5, "memory.search", { created_before: null, hits: [] })]).asOf, null); // the live memory
  assert.equal(memoryTrace(RUN_A_EVENTS).asOf, undefined); // recorded before created_before existed
});

test("sample D: the planted memory is marked, ranked by the live check and its restore point shown", () => {
  assert.equal(isPlanted(HIT_PLANT.episode), true);
  assert.equal(hitOrder([HIT_PLANT]), "ranked");
  assert.equal(hitSignals(HIT_PLANT).lexical, 1);
  assert.equal(episodeSummary(HIT_PLANT.episode).method, null);
  const trace = memoryTrace(RUN_D_EVENTS);
  assert.deepEqual(trace.readable, ["train", "live"]);
  assert.equal(trace.checks, 2);
  assert.deepEqual(trace.restorePoints, ["v2.2.7"]);
});
