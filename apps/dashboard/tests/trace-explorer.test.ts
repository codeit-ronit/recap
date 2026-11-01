/**
 * Tests for src/view/timeline.ts: how the Trace Explorer pages the trace, cuts it into the run's
 * story, groups each agent's steps, splits a model request's time and places agent turns on a
 * round's axis. Run with `npm test` (Node runs TypeScript directly, see format.test.ts).
 *
 * The entries follow GET /runs/{id}/timeline as the API serves a recorded two-round replay of a
 * real qwen3:8b run (two specialists in parallel), a scripted run, a budget stop and a fix that
 * did not recover the system.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import type { AgentLane, ModelRequest, RoundInfo, TimelineEntry, TimelineView } from "../src/dto.ts";
import {
  ANALYSIS_LIMIT,
  EMPTY_LOG,
  EMPTY_TRACE,
  KIND_LABELS,
  NO_FILTERS,
  TIMELINE_KINDS,
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
  isTimelineKind,
  itemKind,
  kindLabel,
  laneKey,
  laneName,
  laneRank,
  lanesOf,
  logQuery,
  mergeEntries,
  offsetOf,
  readLogFilters,
  requestSplit,
  requestTotals,
  roundGeometry,
  segmentsOf,
  timeOf,
  toTraceEvent,
  traceNext,
  traceStart,
  turnToolCalls,
} from "../src/view/timeline.ts";

const TEL = "telemetry_investigator";
const INFRA = "infrastructure_investigator";
const T0 = Date.parse("2026-10-26T05:01:36.500Z");

/** An entry builder: increasing seq, `at` ms after T0 (default: seq ms). */
function tracer() {
  let seq = 0;
  return (over: Partial<TimelineEntry> & Pick<TimelineEntry, "category" | "kind"> & { at?: number }): TimelineEntry => {
    seq += 1;
    const { at, ...rest } = over;
    return {
      seq,
      ts: new Date(T0 + (at ?? seq)).toISOString(),
      stage: "INVESTIGATING",
      agent: null,
      round: null,
      step: null,
      tool: null,
      invocation_id: null,
      verdict: null,
      ok: null,
      evidence_ids: [],
      duration_ms: null,
      model_request: null,
      untrusted_text: false,
      payload: {},
      ...rest,
    };
  };
}

function request(over: Partial<ModelRequest> = {}): ModelRequest {
  return {
    provider: "ollama",
    model: "qwen3:8b",
    model_reported: "qwen3:8b",
    origin: "replay",
    purpose: "investigate",
    mode: "tools",
    attempt: 1,
    retry: false,
    status: 200,
    ok: true,
    response_kind: "tool_request",
    input_tokens: 2134,
    output_tokens: 72,
    duration_ms: 24178.7,
    server_ms: 24133.4,
    outside_server_ms: 45.3,
    server_compute_ms: 18325.1,
    server_wait_ms: 5808.3,
    replayed: true,
    finish_reason: "stop",
    ...over,
  };
}

function lane(over: Partial<AgentLane> & Pick<AgentLane, "agent" | "round">): AgentLane {
  return {
    mode: "parallel",
    started_at: "2026-10-26T05:01:36.968975Z",
    finished_at: "2026-10-26T05:01:37.012767Z",
    duration_ms: 43.8,
    outcome: "hypothesis",
    partial: false,
    error: null,
    steps: 4,
    tool_calls: 6,
    model_requests: 0,
    input_tokens: null,
    output_tokens: null,
    model_request_ms: 145707.5,
    model_server_ms: 145542.6,
    evidence_ids: [],
    hypotheses: 2,
    ...over,
  };
}

function info(over: Partial<RoundInfo> & Pick<RoundInfo, "round">): RoundInfo {
  return {
    mode: "parallel",
    rule_id: "RT0-forced",
    reason: "parallel specialists forced by run configuration",
    agents: [TEL, INFRA],
    focus_services: [],
    open_questions: [],
    leads: [],
    escalation: false,
    wall_ms: 48.25,
    sum_ms: 90.01,
    overlap_ms: 41.76,
    model_request_ms: null,
    verification_status: "insufficient",
    ...over,
  };
}

function page(entries: TimelineEntry[], over: Partial<TimelineView> = {}): TimelineView {
  return {
    schema_version: "recap-api-v1",
    run_id: "run-test",
    status: "finished",
    state: "RESOLVED",
    live: false,
    trace_available: true,
    total_events: entries.length,
    after_seq: 0,
    limit: 2000,
    filters: { agent: null, round: null, kind: null, tool: null },
    matching_events: entries.length,
    next_after_seq: entries.length ? entries[entries.length - 1].seq : 0,
    has_more: false,
    entries,
    lanes: [],
    rounds: [],
    counts: {},
    retries: { model_retries: 0, failed_model_requests: 0, fix_attempts: 0 },
    notes: [],
    not_recorded: [],
    ...over,
  };
}

/** A run with three rounds: an inconclusive round, a supported one, a fix the oracle rejected, a third round and a second fix. */
function threeRoundRun(): TimelineEntry[] {
  const e = tracer();
  return [
    e({ category: "state.transition", kind: "lifecycle", stage: "CREATED", payload: { from: null, to: "CREATED", incident: { service: "frontend", symptoms: ["frontend: slow"] } }, untrusted_text: true }),
    e({ category: "tool.discovery", kind: "tool_discovery", stage: "CREATED" }),
    e({ category: "memory.search", kind: "memory", stage: "RETRIEVING", payload: { hits: [] } }),
    e({ category: "routing.decision", kind: "routing", round: 1 }),
    e({ category: "agent.start", kind: "agent_start", round: 1, agent: TEL }),
    // An event without a round between two events of round 1 stays in round 1.
    e({ category: "memory.assessment", kind: "memory", round: null }),
    e({ category: "agent.finish", kind: "agent_finish", round: 1, agent: TEL }),
    e({ category: "verification.evidence", kind: "verification", round: 1, verdict: "insufficient" }),
    e({ category: "routing.decision", kind: "routing", round: 2 }),
    e({ category: "verification.evidence", kind: "verification", round: 2, verdict: "supported" }),
    e({ category: "state.transition", kind: "lifecycle", stage: "PROPOSING", payload: { from: "INVESTIGATING", to: "PROPOSING", reason: "diagnosis: cart v2.1.0 broke cart" } }),
    e({ category: "mitigation.executed", kind: "write_executed", stage: "MITIGATING", ok: true }),
    e({ category: "verification.recovery", kind: "oracle", stage: "VERIFYING", ok: false, verdict: "not_verified" }),
    e({ category: "state.transition", kind: "lifecycle", stage: "INVESTIGATING", payload: { from: "VERIFYING", to: "INVESTIGATING" } }),
    e({ category: "routing.decision", kind: "routing", round: 3 }),
    e({ category: "verification.evidence", kind: "verification", round: 3, verdict: "supported" }),
    e({ category: "verification.recovery", kind: "oracle", stage: "VERIFYING", ok: true, verdict: "verified" }),
    e({ category: "run.summary", kind: "summary", stage: "RESOLVED" }),
  ];
}

// --- the run's story -------------------------------------------------------------------------------

test("the trace is cut into setup, rounds and what followed each round, in order", () => {
  const segments = segmentsOf(threeRoundRun());
  assert.deepEqual(
    segments.map((s) => [s.kind, s.round, s.after, s.entries.map((x) => x.seq)]),
    [
      ["setup", null, null, [1, 2, 3]],
      ["round", 1, null, [4, 5, 6, 7, 8]],
      ["round", 2, null, [9, 10]],
      ["after", null, 2, [11, 12, 13, 14]],
      ["round", 3, null, [15, 16]],
      ["after", null, 3, [17, 18]],
    ],
  );
  // Out of order input is read in seq order; nothing is lost or duplicated.
  const shuffled = [...threeRoundRun()].reverse();
  assert.deepEqual(segmentsOf(shuffled).flatMap((s) => s.entries.map((x) => x.seq)), Array.from({ length: 18 }, (_, i) => i + 1));
  assert.deepEqual(segmentsOf([]), []);
});

test("a round's escalation reason comes from what the trace recorded before it", () => {
  const segments = segmentsOf(threeRoundRun());
  const rounds = [info({ round: 1, verification_status: "insufficient" }), info({ round: 2 }), info({ round: 3 })];
  assert.deepEqual(escalationOf(info({ round: 1, escalation: false }), segments, 1, rounds), { kind: "none" });
  assert.deepEqual(escalationOf(info({ round: 2, escalation: true }), segments, 2, rounds), { kind: "inconclusive", round: 1, status: "insufficient" });
  // The fix before round 3 did not recover the system (the oracle said so).
  assert.deepEqual(escalationOf(info({ round: 3, escalation: true }), segments, 4, rounds), { kind: "failed_fix" });
  // Not recorded stays not recorded; an escalation with no earlier segment says only that it was recorded.
  assert.deepEqual(escalationOf(info({ round: 1, escalation: null }), segments, 1, rounds), { kind: "not_recorded" });
  assert.deepEqual(escalationOf(undefined, segments, 1, rounds), { kind: "not_recorded" });
  assert.deepEqual(escalationOf(info({ round: 1, escalation: true }), segments, 0, rounds), { kind: "recorded" });
  // The previous round's verification status is unknown: null, never a guess.
  assert.deepEqual(escalationOf(info({ round: 2, escalation: true }), segments, 2, []), { kind: "inconclusive", round: 1, status: null });
});

// --- an agent's turn, step by step ------------------------------------------------------------------

function parallelRound(): TimelineEntry[] {
  const e = tracer();
  return [
    e({ category: "agent.start", kind: "agent_start", round: 1, agent: TEL }),
    e({ category: "context.pack", kind: "context", round: 1, agent: TEL, step: 1, payload: { step: 1, tokens_before: 377, tokens_after: 377, budget_tokens: 3000, items: ["shop-architecture#services-1"], dropped: [], estimate: true } }),
    e({ category: "llm.call", kind: "model_request", round: 1, agent: TEL, step: 1, model_request: request() }),
    e({ category: "agent.decision", kind: "agent_decision", round: 1, agent: TEL, step: 1, payload: { step: 1, action: "call_tools" } }),
    e({ category: "policy.decision", kind: "policy", round: 1, agent: TEL, tool: "get_service_metrics", verdict: "ALLOW", payload: { tool: "get_service_metrics", verdict: "ALLOW" } }),
    e({ category: "tool.call", kind: "tool_call", round: 1, agent: TEL, tool: "get_service_metrics", invocation_id: "call-a" }),
    // The other specialist starts while telemetry's call is in flight.
    e({ category: "agent.start", kind: "agent_start", round: 1, agent: INFRA }),
    e({ category: "context.pack", kind: "context", round: 1, agent: INFRA, step: 1 }),
    e({ category: "llm.call", kind: "model_request", round: 1, agent: INFRA, step: 1, model_request: request({ server_wait_ms: 32571.8 }) }),
    e({ category: "agent.decision", kind: "agent_decision", round: 1, agent: INFRA, step: 1 }),
    e({ category: "policy.decision", kind: "policy", round: 1, agent: INFRA, tool: "delete_namespace", verdict: "DENY", payload: { tool: "delete_namespace", verdict: "DENY" } }),
    e({ category: "policy.decision", kind: "policy", round: 1, agent: TEL, tool: "get_service_metrics", verdict: "ALLOW" }),
    e({ category: "tool.call", kind: "tool_call", round: 1, agent: TEL, tool: "get_service_metrics", invocation_id: "call-b" }),
    // Results arrive out of order: paired by invocation id, not by position.
    e({ category: "tool.result", kind: "tool_result", round: 1, agent: TEL, tool: "get_service_metrics", invocation_id: "call-b", ok: true, evidence_ids: ["ev-f9395a-r1t02"] }),
    e({ category: "tool.result", kind: "tool_result", round: 1, agent: TEL, tool: "get_service_metrics", invocation_id: "call-a", ok: true, evidence_ids: ["ev-f9395a-r1t01"] }),
    e({ category: "safety.injection_flag", kind: "injection_flag", round: 1, agent: INFRA, payload: { evidence_id: "ev-x", patterns: ["ignore-instructions"] } }),
    // Step 2 of telemetry: a failed answer, then a corrective retry, both numbered step 2.
    e({ category: "context.pack", kind: "context", round: 1, agent: TEL, step: 2 }),
    e({ category: "llm.call", kind: "model_request", round: 1, agent: TEL, step: 2, model_request: request({ ok: false }) }),
    e({ category: "llm.call", kind: "model_request", round: 1, agent: TEL, step: 2, model_request: request({ attempt: 2, retry: true }) }),
    e({ category: "agent.decision", kind: "agent_decision", round: 1, agent: TEL, step: 2, payload: { step: 2, action: "finish" } }),
    e({ category: "agent.finish", kind: "agent_finish", round: 1, agent: TEL }),
    // RECAP's own events are not an agent's steps.
    e({ category: "orchestration.summary", kind: "round_summary", round: 1 }),
    e({ category: "policy.decision", kind: "policy", round: 1, agent: "controller", tool: "restart_service", verdict: "APPROVAL_REQUIRED" }),
  ];
}

test("each agent's turn groups into steps: context, model requests, decision, then its tool calls", () => {
  const turns = agentTurns(parallelRound());
  assert.deepEqual([...turns.keys()], [TEL, INFRA]);
  const tel = turns.get(TEL)!;
  assert.equal(tel.start?.seq, 1);
  assert.equal(tel.finish?.seq, 21);
  assert.deepEqual(tel.steps.map((s) => s.step), [1, 2]);
  const [first, second] = tel.steps;
  assert.equal(first.context?.seq, 2);
  assert.deepEqual(first.requests.map((r) => r.seq), [3]);
  assert.equal(first.decision?.seq, 4);
  // Each verdict pairs with the next call of the same tool; each result with its call by id.
  assert.deepEqual(
    first.tools.map((t) => [t.policy?.seq, t.call?.seq, t.result?.seq, t.result?.evidence_ids[0]]),
    [
      [5, 6, 15, "ev-f9395a-r1t01"],
      [12, 13, 14, "ev-f9395a-r1t02"],
    ],
  );
  // A failed answer and its retry share one step.
  assert.deepEqual(second.requests.map((r) => r.model_request?.attempt), [1, 2]);
  assert.equal(second.decision?.seq, 20);
  assert.equal(turnToolCalls(tel), 2);

  const infra = turns.get(INFRA)!;
  assert.equal(infra.steps.length, 1);
  // A denial has no call after it: it stands alone, never paired with another call.
  assert.deepEqual(infra.steps[0].tools.map((t) => [t.tool, t.policy?.verdict, t.call, t.result]), [["delete_namespace", "DENY", null, null]]);
  assert.deepEqual(infra.steps[0].other.map((o) => o.kind), ["injection_flag"]);
  assert.equal(turnToolCalls(infra), 0);
});

test("a verdict with no call after it (a budget stop cut the trace) is shown, not dropped; a result with no id pairs by tool", () => {
  const e = tracer();
  const turns = agentTurns([
    e({ category: "context.pack", kind: "context", round: 1, agent: "investigator", step: 4 }),
    e({ category: "agent.decision", kind: "agent_decision", round: 1, agent: "investigator", step: 4 }),
    e({ category: "policy.decision", kind: "policy", round: 1, agent: "investigator", tool: "get_service_logs", verdict: "ALLOW" }),
    e({ category: "tool.call", kind: "tool_call", round: 1, agent: "investigator", tool: "get_service_logs" }),
    e({ category: "tool.result", kind: "tool_result", round: 1, agent: "investigator", tool: "get_service_logs", ok: false }),
    e({ category: "policy.decision", kind: "policy", round: 1, agent: "investigator", tool: "get_recent_deployments", verdict: "ALLOW" }),
  ]);
  const step = turns.get("investigator")!.steps[0];
  assert.deepEqual(
    step.tools.map((t) => [t.tool, t.policy?.seq ?? null, t.call?.seq ?? null, t.result?.seq ?? null]),
    [
      ["get_service_logs", 3, 4, 5],
      ["get_recent_deployments", 6, null, null],
    ],
  );
  // A tool event before any step opens one (numbered null: the trace did not number it).
  const early = agentTurns([e({ category: "tool.call", kind: "tool_call", round: 1, agent: TEL, tool: "list_services" })]);
  assert.equal(early.get(TEL)!.steps[0].step, null);
  assert.equal(early.get(TEL)!.steps[0].tools[0].tool, "list_services");
});

// --- model requests ---------------------------------------------------------------------------------

test("a model request's time splits into compute, waiting inside the server and outside it, as recorded", () => {
  const full = requestSplit(request());
  assert.equal(full.totalMs, 24178.7);
  assert.deepEqual(full.parts.map((p) => p.key), ["compute", "wait", "outside"]);
  assert.equal(full.waitMs, 5808.3);
  assert.ok(Math.abs(full.parts.reduce((s, p) => s + p.share, 0) - 1) < 0.01);
  // A server that reports its time but not its compute: one server part, no invented wait.
  const noCompute = requestSplit(request({ server_compute_ms: null, server_wait_ms: null }));
  assert.deepEqual(noCompute.parts.map((p) => p.key), ["server", "outside"]);
  assert.equal(noCompute.waitMs, null);
  assert.equal(noCompute.computeMs, null);
  // No server time at all (a hosted API): the whole request, not broken down.
  const hosted = requestSplit(request({ server_ms: null, outside_server_ms: null, server_compute_ms: null, server_wait_ms: null }));
  assert.deepEqual(hosted.parts.map((p) => [p.key, p.share]), [["total", 1]]);
  assert.equal(hosted.serverMs, null);
  // Zero time: no part claims a share.
  assert.deepEqual(requestSplit(request({ duration_ms: 0, server_ms: null, outside_server_ms: null })).parts.map((p) => p.share), [0]);
});

test("request totals add up only what the server reported", () => {
  const e = tracer();
  const entries = [
    e({ category: "llm.call", kind: "model_request", agent: TEL, model_request: request() }),
    e({ category: "llm.call", kind: "model_request", agent: TEL, model_request: request({ input_tokens: null, output_tokens: null, ok: false, server_compute_ms: null, server_wait_ms: null }) }),
    e({ category: "llm.call", kind: "model_request", agent: TEL, model_request: request({ attempt: 2, retry: true, server_wait_ms: 1000 }) }),
    e({ category: "tool.call", kind: "tool_call", agent: TEL }),
  ];
  const totals = requestTotals(entries);
  assert.equal(totals.count, 3);
  assert.equal(totals.retries, 1);
  assert.equal(totals.failed, 1);
  assert.equal(totals.replayed, 3);
  assert.equal(totals.unreported, 1);
  assert.equal(totals.inputTokens, 4268);
  assert.equal(totals.outputTokens, 144);
  assert.equal(totals.withWait, 2);
  assert.ok(Math.abs((totals.waitMs ?? 0) - 6808.3) < 1e-6);
  // No requests (a scripted run): tokens and server time stay null, not zero.
  const none = requestTotals([e({ category: "tool.call", kind: "tool_call" })]);
  assert.deepEqual([none.count, none.inputTokens, none.outputTokens, none.serverMs, none.waitMs], [0, null, null, null, null]);
  assert.equal(allReplayed(entries), true);
  assert.equal(allReplayed([...entries, e({ category: "llm.call", kind: "model_request", model_request: request({ replayed: false }) })]), false);
  assert.equal(allReplayed([]), false);
});

// --- agent turns on a round's axis -----------------------------------------------------------------

test("turns are placed on the round's own axis; the overlap window comes from their recorded times", () => {
  const lanes = [
    lane({ agent: INFRA, round: 1, started_at: "2026-10-26T05:01:36.971192Z", finished_at: "2026-10-26T05:01:37.017027Z", duration_ms: 45.8 }),
    lane({ agent: TEL, round: 1 }),
  ];
  const geometry = roundGeometry(lanes, [TEL, INFRA])!;
  // In the router's order, whatever order the server listed them in.
  assert.deepEqual(geometry.bars.map((b) => [b.agent, b.start, b.end]), [
    [TEL, 0, 44],
    [INFRA, 3, 49],
  ]);
  assert.equal(geometry.span, 49);
  assert.deepEqual(geometry.overlap, { start: 3, end: 44 });
  // Back to back: no overlap.
  const sequential = roundGeometry([
    lane({ agent: TEL, round: 2, started_at: "2026-10-26T05:01:37.019680Z", finished_at: "2026-10-26T05:01:37.022536Z" }),
    lane({ agent: INFRA, round: 2, started_at: "2026-10-26T05:01:37.022704Z", finished_at: "2026-10-26T05:01:37.039922Z" }),
  ])!;
  assert.equal(sequential.overlap, null);
  // A turn that never finished has no end and no overlap is claimed.
  const open = roundGeometry([lane({ agent: TEL, round: 1 }), lane({ agent: INFRA, round: 1, finished_at: null, duration_ms: null })])!;
  assert.equal(open.bars.find((b) => b.agent === INFRA)?.end, null);
  assert.equal(open.overlap, null);
  assert.equal(roundGeometry([]), null);
  assert.equal(roundGeometry([lane({ agent: TEL, round: 1, started_at: "not a time" })]), null);
  assert.deepEqual(lanesOf([lane({ agent: TEL, round: 1 }), lane({ agent: TEL, round: 2 })], 2).map((l) => l.round), [2]);
});

test("the longest measured overlap, or null when no round recorded one", () => {
  assert.equal(bestOverlap([info({ round: 1, overlap_ms: 41.76 }), info({ round: 2, overlap_ms: 0 })]), 41.76);
  assert.equal(bestOverlap([info({ round: 1, overlap_ms: null })]), null);
  assert.equal(bestOverlap([]), null);
});

// --- context packs ---------------------------------------------------------------------------------

test("a context pack reads its budget and items; tokens are RECAP's estimate", () => {
  const pack = contextPack({
    step: 2,
    payload: { step: 2, tokens_before: 506, tokens_after: 480, budget_tokens: 3000, items: ["ev-f9395a-r1t01", "shop-architecture#services-1", 7], dropped: ["ev-old"], estimate: true },
  });
  assert.deepEqual(pack, {
    step: 2,
    stage: null,
    tokensBefore: 506,
    tokensAfter: 480,
    budget: 3000,
    items: ["ev-f9395a-r1t01", "shop-architecture#services-1", "7"],
    dropped: ["ev-old"],
    estimate: true,
  });
  const proposal = contextPack({ step: null, payload: { stage: "proposal" } });
  assert.deepEqual([proposal.stage, proposal.tokensAfter, proposal.budget, proposal.items], ["proposal", null, null, []]);
  assert.equal(itemKind("ev-f9395a-r2i02"), "evidence");
  assert.equal(itemKind("runbook-memory-pressure#remediation-1"), "runbook");
  assert.equal(itemKind("ep-12ab"), "other");
});

// --- paging -------------------------------------------------------------------------------------------

test("the whole trace loads page by page, then a live run is polled and a finished one stops", () => {
  const e = tracer();
  const all = Array.from({ length: 5 }, (_, i) => e({ category: "tool.call", kind: "tool_call", round: 1, at: i }));
  assert.equal(traceNext(EMPTY_TRACE), "page");
  const one = addTracePage(EMPTY_TRACE, page(all.slice(0, 2), { has_more: true, next_after_seq: 2, total_events: 5 }));
  assert.deepEqual([one.cursor, one.complete, one.entries.length, traceNext(one)], [2, false, 2, "page"]);
  const two = addTracePage(one, page(all.slice(2), { has_more: false, next_after_seq: 5, total_events: 5 }));
  assert.deepEqual([two.cursor, two.complete, two.entries.map((x) => x.seq), traceNext(two)], [5, true, [1, 2, 3, 4, 5], "stop"]);
  // A live run: keep polling after the last event; an empty poll keeps the cursor.
  const live = addTracePage(two, page([], { live: true, next_after_seq: 5 }));
  assert.equal(traceNext(live), "poll");
  assert.equal(live.cursor, 5);
  assert.equal(live.entries, two.entries); // nothing new: the same array, no re-render
  // Past the analysis limit the loader stops (the event log still pages through everything).
  const capped = addTracePage(EMPTY_TRACE, page(all, { has_more: true }), 3);
  assert.deepEqual([capped.truncated, traceNext(capped)], [true, "stop"]);
  assert.ok(ANALYSIS_LIMIT >= 1000);
});

test("pages merge by seq: no duplicates, in order", () => {
  const e = tracer();
  const [a, b, c] = [e({ category: "x", kind: "other" }), e({ category: "x", kind: "other" }), e({ category: "x", kind: "other" })];
  assert.deepEqual(mergeEntries([a, c], [b, c]).map((x) => x.seq), [1, 2, 3]);
  const same = [a, b];
  assert.equal(mergeEntries(same, []), same);
  assert.equal(mergeEntries(same, [a]), same);
});

test("the event log pages with the server's filters, and a bad filter value is dropped", () => {
  const params = new URLSearchParams("run=run-x&agent=%20telemetry_investigator%20&round=2&kind=model_request&tool=get_service_logs");
  const filters = readLogFilters((key) => params.get(key));
  assert.deepEqual(filters, { agent: TEL, round: 2, kind: "model_request", tool: "get_service_logs" });
  assert.equal(activeLogFilters(filters), 4);
  assert.deepEqual(logQuery(filters, 40), { after_seq: 40, limit: 100, agent: TEL, round: 2, kind: "model_request", tool: "get_service_logs" });
  const bad = new URLSearchParams("round=0&kind=<script>&agent=&tool=");
  assert.deepEqual(readLogFilters((key) => bad.get(key)), NO_FILTERS);
  assert.deepEqual(readLogFilters((key) => new URLSearchParams("round=1.5").get(key)).round, null);
  assert.deepEqual(readLogFilters((key) => new URLSearchParams("round=abc").get(key)).round, null);
  assert.equal(activeLogFilters(NO_FILTERS), 0);
  assert.deepEqual(logQuery(NO_FILTERS, 0, 50), { after_seq: 0, limit: 50, agent: undefined, round: undefined, kind: undefined, tool: undefined });

  const e = tracer();
  const first = addLogPage(EMPTY_LOG, page([e({ category: "x", kind: "other" })], { has_more: true, matching_events: 3, total_events: 10, next_after_seq: 1 }));
  assert.deepEqual([first.loaded, first.cursor, first.hasMore, first.matching, first.total, first.entries.length], [true, 1, true, 3, 10, 1]);
  const second = addLogPage(first, page([e({ category: "x", kind: "other" }), e({ category: "x", kind: "other" })], { has_more: false, matching_events: 3, total_events: 10, next_after_seq: 3, live: true }));
  assert.deepEqual([second.cursor, second.hasMore, second.entries.map((x) => x.seq), second.live], [3, false, [1, 2, 3], true]);
});

// --- labels and small readers -------------------------------------------------------------------

test("every kind the API sends has a label; lanes have names, keys and an order", () => {
  for (const kind of TIMELINE_KINDS) assert.ok(KIND_LABELS[kind].length > 0, kind);
  assert.equal(kindLabel("model_request"), "Model request");
  assert.equal(kindLabel("brand_new_kind"), "brand new kind");
  assert.equal(kindLabel(null), "Other");
  assert.equal(isTimelineKind("oracle"), true);
  assert.equal(isTimelineKind("constructor"), false);
  assert.equal(laneName(TEL), "Telemetry");
  assert.equal(laneName(INFRA), "Infrastructure");
  assert.equal(laneName("investigator"), "Investigator");
  assert.equal(laneName(null), "Controller");
  assert.equal(laneName("controller"), "Controller");
  assert.equal(laneKey(TEL), "telemetry");
  assert.equal(laneKey("something_else"), "other");
  assert.ok(laneRank(TEL) < laneRank(INFRA) && laneRank(INFRA) < laneRank("controller") && laneRank("controller") < laneRank("other_agent"));
});

test("a lifecycle reason naming the diagnosis is split from the claim it repeats", () => {
  assert.deepEqual(diagnosisReason("diagnosis: Inventory's resource pressure and OOMKilled events"), {
    lead: "diagnosis",
    claim: "Inventory's resource pressure and OOMKilled events",
  });
  assert.equal(diagnosisReason("approval required for restart_service"), null);
});

test("offsets count from the first event; entries become TraceEvents for the older helpers", () => {
  const e = tracer();
  const entries = [e({ category: "state.transition", kind: "lifecycle", at: 0 }), e({ category: "tool.call", kind: "tool_call", at: 440 })];
  const start = traceStart(entries);
  assert.equal(start, T0);
  assert.equal(offsetOf(entries[1], start), 440);
  assert.equal(offsetOf(entries[1], null), null);
  assert.equal(traceStart([{ ts: "garbage" }]), null);
  assert.equal(timeOf("2026-10-26T05:01:36.968975Z"), Date.parse("2026-10-26T05:01:36.968Z"));
  const event = toTraceEvent(entries[1], "run-x");
  assert.deepEqual([event.run_id, event.seq, event.category, event.event_id, event.usage], ["run-x", 2, "tool.call", "run-x#2", null]);
});

test("filter options count the loaded events per agent, round and tool", () => {
  const counts = filterCounts(parallelRound());
  assert.equal(counts.agents.get(TEL), 15);
  assert.equal(counts.agents.get(INFRA), 6);
  assert.equal(counts.agents.get("controller"), 1);
  assert.deepEqual([...counts.rounds], [[1, 23]]);
  assert.equal(counts.tools.get("get_service_metrics"), 6);
  assert.equal(counts.tools.get("delete_namespace"), 1);
});
