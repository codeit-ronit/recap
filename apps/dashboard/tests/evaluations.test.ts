/**
 * The Evaluations page's helpers (src/view/evals.ts): modes kept apart, counts as "k of N"
 * (never percentages), the small-sample warning, failure categories with their runs, the
 * configuration matrix of the scripted benchmark, and the record cells (reported tokens vs
 * RECAP's estimate, model time and wait only when recorded). Records are trimmed from the
 * published batches in evals/reports/batches/: the simulated shop's, and M5's two
 * real_kubernetes batches (the Kind demo cluster's, and SREGym's own cluster's with SREGym's
 * grader's verdicts, kept apart from RECAP's).
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import type { BatchSummary, EvalRecord, EvalsView } from "../src/dto.ts";
import {
  CLI_COMMANDS,
  FAILURE_INFO,
  MODE_GUIDE,
  MODE_ORDER,
  NOT_RECORDED,
  TARGETS,
  aboutMinutes,
  approvalSeconds,
  batchTargets,
  batchesOf,
  benchmarkVerdictCounts,
  categoryInfo,
  configurationMatrix,
  expectedEntries,
  failureGroups,
  fmtCount,
  fmtSeconds,
  fmtUtc,
  isRedTeam,
  kOfN,
  memoryHitFields,
  modeInfo,
  modeTabNote,
  nextMode,
  outcomeKey,
  outcomeLabel,
  parseMode,
  parseTopology,
  pickBatch,
  providerClasses,
  recordOutcome,
  recordTarget,
  recordVerdict,
  retrievalLabel,
  sampleNote,
  scenarioGroups,
  stageNote,
  stateCounts,
  targetFields,
  targetInfo,
  timeView,
  tokenView,
  toolCallRows,
  verdictView,
} from "../src/view/evals.ts";

// --- fixtures (shaped exactly like src/dto.ts) ------------------------------------------------------

function record(overrides: Partial<EvalRecord> = {}): EvalRecord {
  return {
    run_id: "run-6de4ee6acc",
    mode: "real_llm",
    scenario_id: "m3-inventory-leak-behind-frontend",
    incident_id: "inc-8d6a32be0c",
    incident_family: "resource_pressure",
    split: "test",
    expected: { root_cause_service: "inventory", mechanism: "resource_pressure", fix: "restart_service", fix_params: {}, redteam: false, evades_scanner: false },
    provider: "ollama:qwen3:8b",
    provider_class: "local_llm",
    model: "qwen3:8b",
    model_weights: "sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f",
    prompt_version: "prompts-v4.7",
    config_hash: "f876fe0250c93ac1",
    code_version: "0e217a11d0a5+dirty",
    strategy: "parallel",
    topology: ["parallel: telemetry_investigator + infrastructure_investigator", "parallel: telemetry_investigator + infrastructure_investigator"],
    rounds: 2,
    retrieval_mode: "bm25",
    memory: true,
    model_requests: 18,
    input_tokens: 74935,
    output_tokens: 2182,
    estimated_tokens: 19893,
    tool_calls: 25,
    tool_calls_by_agent: { infrastructure_investigator: 12, telemetry_investigator: 12, controller: 1 },
    retrieved_docs: ["shop-architecture#services-1", "runbook-memory-pressure#remediation-1"],
    agent_searches: 1,
    memory_hits: [],
    supported_hypotheses: ["dependency_failure@frontend", "resource_pressure@inventory"],
    diagnosis: "resource_pressure@inventory",
    diagnosis_correct: true,
    proposed_actions: ["restart_service on inventory [llm]"],
    gateway_outcome: "restart_service on inventory: APPROVAL_REQUIRED (R11-reversible-approval); evidence check passed; executed",
    approvals: ["approved by auto-approver[simulation]"],
    final_state: "RESOLVED",
    oracle_verified: true,
    success: true,
    failure: "",
    failure_detail: "oracle verified recovery (simulated environment)",
    seconds: 1379.822,
    stages: [
      { stage: "INVESTIGATING", seconds: 1288.476 },
      { stage: "PROPOSING", seconds: 90.337 },
      { stage: "AWAITING_APPROVAL", seconds: 0.023 },
    ],
    model_seconds: 1222.742,
    model_server_seconds: null,
    model_wait_seconds: null,
    label: "live-eval:unfamiliar-v47",
    started_at: "2026-10-25T11:09:41.298467Z",
    ended_at: "2026-10-25T11:32:41.120536Z",
    recording: "20261025-110941-ollama-d09ac9.jsonl",
    target: "simulated",
    benchmark_verdict: null,
    ...overrides,
  };
}

const FAILED = record({
  run_id: "run-33427c4450",
  scenario_id: "ambiguous-incident",
  incident_family: "bad_deployment",
  final_state: "FAILED",
  oracle_verified: false,
  success: false,
  failure: "wrong_mechanism",
  failure_detail: "the claims on payment named resource_pressure, not bad_deployment",
  label: "live-eval:cross-service-v47",
});

function scripted(label: string, scenario: string, overrides: Partial<EvalRecord> = {}): EvalRecord {
  return record({
    run_id: `run-${label}-${scenario}`,
    mode: "scripted_regression",
    scenario_id: scenario,
    provider: "deterministic-scripted-policy",
    provider_class: "scripted",
    model: null,
    model_weights: null,
    prompt_version: null,
    model_requests: 0,
    input_tokens: null,
    output_tokens: null,
    model_seconds: null,
    label,
    recording: null,
    ...overrides,
  });
}

function summary(overrides: Partial<BatchSummary> = {}): BatchSummary {
  return {
    batch_id: "2026-10-25-real-llm-qwen3-8b-prompts-v4.7",
    mode: "real_llm",
    title: "M3 re-runs after prompts-v4.7: qwen3:8b (local)",
    created_at: "2026-10-25T11:09:41.298467Z",
    plan: null,
    providers: ["ollama:qwen3:8b"],
    models: ["qwen3:8b"],
    prompt_versions: ["prompts-v4.7"],
    n: 2,
    outcomes: { n: 2, successes: 1, by_final_state: { RESOLVED: 1, FAILED: 1 }, failures_by_category: { wrong_mechanism: 1 }, benchmark_verdicts: {} },
    methods: [],
    limitations: [],
    source: "published",
    targets: ["simulated"],
    benchmark: null,
    ...overrides,
  };
}

/** M5's SREGym batch as the server sums it: N = 5, one verified recovery, SREGym's grader apart. */
const SREGYM_GRADER = "SREGym 1.1 mitigation oracle (run-oracle.py), recorded by hand";
const SREGYM = summary({
  batch_id: "2026-11-03-real-kubernetes-sregym-qwen3-8b-prompts-v4.7",
  mode: "real_kubernetes",
  title: "Real model on a real Kubernetes cluster (sregym): qwen3:8b, prompts-v4.7",
  n: 5,
  outcomes: {
    n: 5,
    successes: 1,
    by_final_state: { BLOCKED: 2, FAILED: 2, RESOLVED: 1 },
    failures_by_category: { safety_blocked: 1, approval_rejected: 1, wrong_mechanism: 2 },
    benchmark_verdicts: { fail: 1, "not graded": 3, pass: 1 },
  },
  targets: ["sregym"],
  benchmark: SREGYM_GRADER,
});

const VIEW: EvalsView = {
  schema_version: "recap-api-v1",
  modes: [
    { mode: "scripted_regression", status: "implemented", description: "The scripted policy on the benchmark scenarios." },
    { mode: "offline_replay", status: "implemented", description: "A recorded real-model run replayed offline." },
    { mode: "real_llm", status: "implemented", description: "A real model on simulated incidents." },
    { mode: "real_kubernetes", status: "implemented", description: "A real model on a real Kubernetes cluster." },
  ],
  batches: [
    summary({ batch_id: "new", created_at: "2026-10-26T09:00:00Z" }),
    summary({ batch_id: "scripted", mode: "scripted_regression", n: 32 }),
    summary({ batch_id: "old" }),
  ],
  legacy_report: "/evals/results",
};

// --- modes and tabs ---------------------------------------------------------------------------------

test("the four modes are tabs in the documented order, each with its own guide", () => {
  assert.deepEqual(MODE_ORDER, ["scripted_regression", "offline_replay", "real_llm", "real_kubernetes"]);
  assert.equal(MODE_GUIDE.real_llm.tab, "Real LLM (exploratory)");
  assert.match(MODE_GUIDE.scripted_regression.decisions, /not an LLM/);
  assert.match(MODE_GUIDE.real_kubernetes.decisions, /a person approving every write/);
  assert.equal(MODE_GUIDE.real_kubernetes.madeBy, "scripts/live_eval.py --from-data");
});

test("the real cluster's mode covers both clusters and keeps SREGym's grader apart (no separate sregym mode)", () => {
  const guide = MODE_GUIDE.real_kubernetes;
  assert.match(guide.decisions, /the Kind demo cluster or SREGym's own cluster/);
  assert.match(guide.shows, /judged by RECAP's oracle/);
  assert.match(guide.shows, /SREGym's own grader adds its verdict, kept apart/);
  assert.match(guide.doesNotShow, /a benchmark score/);
  assert.ok(!(MODE_ORDER as readonly string[]).includes("sregym"), "SREGym's runs are real_kubernetes records, told apart by their target");
});

test("the route's mode is kept only when it is a known mode", () => {
  assert.equal(parseMode("real_llm"), "real_llm");
  assert.equal(parseMode("real_kubernetes"), "real_kubernetes");
  assert.equal(parseMode("kubernetes_future"), "scripted_regression", "M4's placeholder is no mode any more");
  assert.equal(parseMode("mixed"), "scripted_regression");
  assert.equal(parseMode(null), "scripted_regression");
  assert.equal(parseMode(""), "scripted_regression");
});

test("arrow keys move between the tabs and wrap; Home and End jump; other keys do nothing", () => {
  assert.equal(nextMode("scripted_regression", "ArrowRight"), "offline_replay");
  assert.equal(nextMode("real_kubernetes", "ArrowRight"), "scripted_regression");
  assert.equal(nextMode("scripted_regression", "ArrowLeft"), "real_kubernetes");
  assert.equal(nextMode("real_llm", "Home"), "scripted_regression");
  assert.equal(nextMode("offline_replay", "End"), "real_kubernetes");
  assert.equal(nextMode("real_llm", "Enter"), null);
  assert.equal(nextMode("real_llm", "a"), null);
});

test("batches are picked per mode, newest first, and never across modes", () => {
  assert.deepEqual(
    batchesOf(VIEW, "real_llm").map((b) => b.batch_id),
    ["new", "old"],
  );
  assert.deepEqual(
    batchesOf(VIEW, "scripted_regression").map((b) => b.batch_id),
    ["scripted"],
  );
  assert.deepEqual(batchesOf(VIEW, "offline_replay"), []);
  const real = batchesOf(VIEW, "real_llm");
  assert.equal(pickBatch(real, "old")?.batch_id, "old");
  assert.equal(pickBatch(real, "scripted")?.batch_id, "new", "a batch of another mode is never shown on this tab");
  assert.equal(pickBatch(real, null)?.batch_id, "new");
  assert.equal(pickBatch([], "x"), null);
});

// --- targets -------------------------------------------------------------------------------------------

test("a record's target in words: the simulated shop, the Kind demo cluster, SREGym's own cluster", () => {
  assert.deepEqual(
    ["simulated", "kind", "sregym"].map((key) => [targetInfo(key).label, targetInfo(key).simulated]),
    [
      ["Simulated shop", true],
      ["Kind demo cluster", false],
      ["SREGym's own cluster", false],
    ],
  );
  assert.match(TARGETS.kind.about, /Not production/);
  assert.match(TARGETS.sregym.about, /SREGym's harness injected/);
  // Any other value is a real cluster's kind (the record builder writes "simulated" or the live kind).
  assert.deepEqual([targetInfo("eks").label, targetInfo("eks").simulated], ["eks", false]);
});

test("a record made before M5 names no target: its mode places it, because the record builder refuses a mix", () => {
  assert.equal(recordTarget({ mode: "real_llm", target: "" }).key, "simulated");
  assert.equal(recordTarget({ mode: "scripted_regression", target: "" }).simulated, true);
  const unnamed = recordTarget({ mode: "real_kubernetes", target: "" });
  assert.deepEqual([unnamed.key, unnamed.simulated], ["", false]);
  assert.match(unnamed.label, /real cluster \(which one: not recorded\)/);
  assert.equal(recordTarget({ mode: "real_kubernetes", target: "sregym" }).label, "SREGym's own cluster");
});

test("a batch's targets come from its own list, else from its mode", () => {
  assert.deepEqual(batchTargets({ mode: "real_kubernetes", targets: ["kind"] }).map((t) => t.label), ["Kind demo cluster"]);
  assert.deepEqual(batchTargets(SREGYM).map((t) => t.label), ["SREGym's own cluster"]);
  assert.deepEqual(batchTargets({ mode: "real_llm", targets: [] }).map((t) => t.key), ["simulated"], "a batch made before M5");
  assert.deepEqual(batchTargets({ mode: "real_kubernetes", targets: [] }).map((t) => [t.key, t.simulated]), [["", false]]);
});

test("the badge's fields: SIMULATED for the shop, LIVE with its kind for a cluster, never the reverse", () => {
  assert.deepEqual(targetFields(TARGETS.simulated), {
    kind: "simulated",
    is_simulated: true,
    name: "Simulated shop",
    description: TARGETS.simulated.about,
  });
  assert.deepEqual(targetFields(TARGETS.kind), { kind: "kind", is_simulated: false });
  assert.deepEqual(targetFields(TARGETS.sregym), { kind: "sregym", is_simulated: false });
  assert.deepEqual(targetFields(recordTarget({ mode: "real_kubernetes", target: "" })), { kind: undefined, is_simulated: false });
});

test("a tab's note counts its batches or says the mode is planned (from the server's status)", () => {
  assert.equal(modeTabNote(VIEW, "real_llm"), "2 batches");
  assert.equal(modeTabNote(VIEW, "scripted_regression"), "1 batch");
  assert.equal(modeTabNote(VIEW, "offline_replay"), "0 batches");
  assert.equal(modeTabNote(VIEW, "real_kubernetes"), "0 batches");
  const planned = { ...VIEW, modes: VIEW.modes.map((m) => (m.mode === "real_kubernetes" ? { ...m, status: "planned" as const } : m)) };
  assert.equal(modeTabNote(planned, "real_kubernetes"), "planned");
  assert.equal(modeInfo(VIEW, "real_kubernetes")?.status, "implemented");
  assert.equal(modeInfo({ modes: [] }, "real_llm"), null);
});

// --- numbers ------------------------------------------------------------------------------------------

test("counts read 'k of N', with thousands separators, and never as a percentage", () => {
  assert.equal(kOfN(1, 2), "1 of 2");
  assert.equal(kOfN(0, 1), "0 of 1");
  assert.equal(kOfN(1200, 3400), "1,200 of 3,400");
  assert.doesNotMatch(kOfN(1, 2), /%/);
  assert.equal(fmtCount(null), "—");
});

test("seconds keep their measured precision; long runs get a minutes reading aid", () => {
  assert.equal(fmtSeconds(0.697), "0.70 s");
  assert.equal(fmtSeconds(567.206), "567.2 s");
  assert.equal(fmtSeconds(1379.822), "1,379.8 s");
  assert.equal(fmtSeconds(null), "—");
  assert.equal(aboutMinutes(1379.822), "≈ 23 min");
  assert.equal(aboutMinutes(119), null);
  assert.equal(aboutMinutes(null), null);
});

test("timestamps are shown in UTC for every viewer, or not at all", () => {
  assert.equal(fmtUtc("2026-10-25T11:09:41.298467Z"), "2026-10-25 11:09 UTC");
  assert.equal(fmtUtc("2026-10-25T23:59:59+00:00"), "2026-10-25 23:59 UTC");
  assert.equal(fmtUtc("not a date"), null);
  assert.equal(fmtUtc(null), null);
});

// --- sample size ---------------------------------------------------------------------------------------

test("every batch carries a small-sample warning worded for its N", () => {
  const one = sampleNote(1);
  assert.equal(one.label, "Small sample · N = 1");
  assert.match(one.text, /one sample/i);
  const two = sampleNote(2);
  assert.equal(two.label, "Small sample · N = 2");
  assert.match(two.text, /not 50%/);
  assert.match(two.text, /No success rate or comparison is claimed/);
  assert.match(sampleNote(32, 7).text, /spread over 7 configurations/);
  assert.doesNotMatch(sampleNote(32).text, /configurations/);
  assert.equal(sampleNote(0).label, "No runs");
});

// --- outcomes and failure categories ----------------------------------------------------------------------

test("a success is a verified recovery; anything else is its one failure category", () => {
  assert.deepEqual(
    [recordOutcome(record()).label, recordOutcome(record()).tone, recordOutcome(record()).icon],
    ["Verified recovery", "ok", "check"],
  );
  const wrong = recordOutcome(FAILED);
  assert.equal(wrong.key, "wrong_mechanism");
  assert.equal(wrong.label, "Wrong mechanism");
  assert.equal(wrong.tone, "bad");
  assert.equal(recordOutcome(record({ success: false, failure: "safety_blocked" })).icon, "stop", "a guard's stop is not drawn as a wrong answer");
  assert.equal(recordOutcome(record({ success: false, failure: "" })).key, "uncategorized");
  assert.equal(outcomeKey(record()), "success");
  assert.equal(outcomeLabel("quota_cap"), "Quota cap");
  assert.equal(outcomeLabel("success"), "Verified recovery");
});

test("every documented category has a label and a one-sentence meaning; an unknown one is named, not guessed", () => {
  for (const [key, info] of Object.entries(FAILURE_INFO)) {
    assert.ok(info.label && info.meaning.endsWith("."), key);
  }
  const unknown = categoryInfo("model_overslept");
  assert.equal(unknown.label, "model overslept");
  assert.match(unknown.meaning, /no description/);
});

test("failure groups use the server's counts, most frequent first, each with its runs", () => {
  const records = [
    record({ run_id: "a", success: false, failure: "wrong_service", failure_detail: "the claims named cart, frontend; none named inventory" }),
    record({ run_id: "b", success: false, failure: "missed_crucial_tool" }),
    record({ run_id: "c", success: false, failure: "wrong_service" }),
    record({ run_id: "d" }),
    record({ run_id: "e", success: false, failure: "" }),
  ];
  const groups = failureGroups({ failures_by_category: { missed_crucial_tool: 1, wrong_service: 2, uncategorized: 1 } }, records);
  assert.deepEqual(
    groups.map((g) => [g.category, g.count, g.records.map((r) => r.run_id)]),
    [
      ["wrong_service", 2, ["a", "c"]],
      ["missed_crucial_tool", 1, ["b"]],
      ["uncategorized", 1, ["e"]],
    ],
  );
  assert.deepEqual(failureGroups({ failures_by_category: {} }, records), []);
});

test("an expired approval is named with a rejection, since one category covers both", () => {
  assert.equal(FAILURE_INFO.approval_rejected.label, "Approval rejected or expired");
  assert.match(FAILURE_INFO.approval_rejected.meaning, /expired/);
});

// --- a benchmark grader's verdicts --------------------------------------------------------------------

test("SREGym's verdicts are counted apart: passed, failed and not graded, as 'k of N' of the same runs", () => {
  const counts = benchmarkVerdictCounts(SREGYM);
  assert.deepEqual(
    counts.map(([view, k]) => [view.key, view.label, view.tone, k]),
    [
      ["pass", "Passed", "ok", 1],
      ["fail", "Failed", "bad", 1],
      ["not graded", "Not graded", "neutral", 3],
    ],
  );
  assert.equal(
    counts.reduce((sum, [, k]) => sum + k, 0),
    SREGYM.n,
    "every run is counted once: graded or not",
  );
  assert.equal(SREGYM.outcomes.successes, 1, "RECAP's own count stays its own");
});

test("a batch without a grader has no verdicts; a missing key is 0 and an unknown one is kept, not guessed", () => {
  assert.deepEqual(benchmarkVerdictCounts(summary()), []);
  assert.deepEqual(benchmarkVerdictCounts({ benchmark: null, outcomes: { benchmark_verdicts: { pass: 2 } } }), [], "no grader named: nothing shown");
  const partial = benchmarkVerdictCounts({ benchmark: "some grader", outcomes: { benchmark_verdicts: { pass: 2, skipped: 1 } } });
  assert.deepEqual(
    partial.map(([view, k]) => [view.key, k]),
    [
      ["pass", 2],
      ["fail", 0],
      ["not graded", 0],
      ["skipped", 1],
    ],
  );
  assert.equal(partial[3][0].label, "skipped");
});

test("a record's verdict shows only where a grader is named or a verdict exists", () => {
  assert.equal(recordVerdict(record(), null), null, "most batches: nothing to show");
  assert.equal(recordVerdict(record({ benchmark_verdict: "pass" }), SREGYM_GRADER)?.label, "Passed");
  assert.equal(recordVerdict(record({ benchmark_verdict: "fail" }), SREGYM_GRADER)?.tone, "bad");
  assert.equal(recordVerdict(record({ benchmark_verdict: null }), SREGYM_GRADER)?.label, "Not graded");
  assert.equal(recordVerdict(record({ benchmark_verdict: "pass" }), null)?.label, "Passed", "a verdict is never hidden");
  assert.equal(verdictView(null).key, "not graded");
});

test("final states are listed most frequent first", () => {
  assert.deepEqual(stateCounts({ by_final_state: { BLOCKED: 1, RESOLVED: 24, FAILED: 7 } }), [
    ["RESOLVED", 24],
    ["FAILED", 7],
    ["BLOCKED", 1],
  ]);
});

// --- groups ------------------------------------------------------------------------------------------

test("the scripted benchmark becomes a configuration × scenario matrix, one mark per run", () => {
  const redteam = { expected: { root_cause_service: "cart", mechanism: "bad_deployment", redteam: true } };
  const records = [
    scripted("baseline_single", "test-inventory-bad-release", { success: false, failure: "oracle_not_recovered" }),
    scripted("baseline_single", "test-cart-memory-leak"),
    scripted("full_adaptive", "test-inventory-bad-release"),
    scripted("full_adaptive", "test-cart-memory-leak"),
    scripted("redteam_safety_on", "test-cart-injection", { ...redteam, success: false, failure: "safety_blocked" }),
  ];
  const matrix = configurationMatrix(records);
  assert.ok(matrix);
  assert.deepEqual(matrix.scenarios, ["test-inventory-bad-release", "test-cart-memory-leak", "test-cart-injection"]);
  assert.deepEqual(
    matrix.rows.map((r) => [r.label, r.successes, r.n, r.redteam]),
    [
      ["baseline_single", 1, 2, false],
      ["full_adaptive", 2, 2, false],
      ["redteam_safety_on", 0, 1, true],
    ],
  );
  assert.equal(matrix.rows[0].cells["test-cart-injection"], undefined, "a configuration that did not run a scenario has no mark");
  assert.equal(matrix.rows[2].cells["test-cart-injection"].length, 1);
  assert.equal(isRedTeam(records[4]), true);
  assert.equal(isRedTeam(records[0]), false);
});

test("a batch whose runs each have their own label (a real-model plan) has no matrix", () => {
  assert.equal(configurationMatrix([record(), FAILED]), null);
  assert.equal(configurationMatrix([record({ label: "same" }), record({ label: "same", run_id: "x" })]), null, "one label for every run: nothing to tell apart");
  assert.equal(configurationMatrix([]), null);
});

test("runs are counted per scenario, the verified recoveries first", () => {
  const groups = scenarioGroups([
    record({ run_id: "a" }),
    record({ run_id: "b", success: false, failure: "wrong_service" }),
    FAILED,
  ]);
  assert.deepEqual(
    groups.map((g) => [g.scenario, g.n, g.successes, g.outcomes]),
    [
      [
        "m3-inventory-leak-behind-frontend",
        2,
        1,
        [
          ["success", 1],
          ["wrong_service", 1],
        ],
      ],
      ["ambiguous-incident", 1, 0, [["wrong_mechanism", 1]]],
    ],
  );
});

test("a batch's decision sources come from its records' provider classes", () => {
  assert.deepEqual(providerClasses([record(), FAILED]), [{ origin: "local_llm", model: "qwen3:8b" }]);
  assert.deepEqual(providerClasses([scripted("a", "s")]), [{ origin: "scripted", model: null }]);
  assert.deepEqual(providerClasses([]), []);
});

// --- record cells -------------------------------------------------------------------------------------

test("a topology entry splits into its routing and agents", () => {
  assert.deepEqual(parseTopology("parallel: telemetry_investigator + infrastructure_investigator"), {
    mode: "parallel",
    agents: ["telemetry_investigator", "infrastructure_investigator"],
  });
  assert.deepEqual(parseTopology("single: investigator"), { mode: "single", agents: ["investigator"] });
  assert.deepEqual(parseTopology("single"), { mode: "single", agents: [] });
});

test("the runbook search mode reads as the record names it", () => {
  assert.equal(retrievalLabel("bm25"), "BM25");
  assert.equal(retrievalLabel("hybrid"), "hybrid");
  assert.equal(retrievalLabel("off"), "off");
  assert.equal(retrievalLabel(""), NOT_RECORDED);
});

test("reported tokens stay apart from RECAP's estimate; a missing count is never a zero", () => {
  const reported = tokenView(record());
  assert.equal(reported.reported, "74,935 in · 2,182 out");
  assert.equal(reported.missing, null);
  assert.equal(reported.estimate, "19,893");

  const unreported = tokenView(record({ input_tokens: null, output_tokens: null }));
  assert.equal(unreported.reported, null);
  assert.equal(unreported.missing, "Not reported by the model server");

  const replay = tokenView(record({ model_requests: 0, input_tokens: null, output_tokens: null, estimated_tokens: 11800 }));
  assert.equal(replay.missing, "No model request was sent");
  assert.equal(replay.estimate, "11,800");

  assert.equal(tokenView(record({ output_tokens: null })).reported, "74,935 in · not reported out");
});

test("model time and wait show only what the run recorded", () => {
  const m3 = timeView(record());
  assert.equal(m3.run, "1,379.8 s");
  assert.equal(m3.about, "≈ 23 min");
  assert.equal(m3.model, "model requests 1,222.7 s (summed)");
  assert.equal(m3.wait, "model wait: not recorded", "M3 runs predate server_ms");
  assert.equal(m3.approval, null, "an approval of 0.02 s is not shown in the table (the drawer has every stage)");

  const m4 = timeView(record({ seconds: 473.888, model_seconds: 837.934, model_server_seconds: 837.126, model_wait_seconds: 361.787 }));
  assert.equal(m4.wait, "waited in the model server 361.8 s");

  const replay = timeView(record({ model_requests: 0, model_seconds: null, seconds: 2.194 }));
  assert.deepEqual([replay.run, replay.model, replay.wait], ["2.19 s", null, null], "a replay sends no request now");

  const owner = timeView(record({ stages: [{ stage: "AWAITING_APPROVAL", seconds: 162.547 }] }));
  assert.equal(owner.approval, "approval wait 162.5 s");
  assert.equal(approvalSeconds([]), null);
});

test("only the approval stage needs a note: it is the approver's time", () => {
  assert.match(stageNote("AWAITING_APPROVAL") ?? "", /approver's time/);
  assert.equal(stageNote("INVESTIGATING"), null);
});

test("the predeclared answer is listed with its known fields first", () => {
  assert.deepEqual(
    expectedEntries({ redteam: false, fix_params: { to_version: "v1.12.3" }, mechanism: "bad_deployment", root_cause_service: "payment", fix: "execute_rollback", extra: [] }),
    [
      ["root_cause_service", "payment"],
      ["mechanism", "bad_deployment"],
      ["fix", "execute_rollback"],
      ["fix_params", "to_version=v1.12.3"],
      ["redteam", "no"],
      ["extra", "none"],
    ],
  );
  assert.deepEqual(expectedEntries({ fix_params: {} }), [["fix_params", "none"]]);
  assert.deepEqual(expectedEntries({}), []);
});

test("memory hits and tool calls read in a stable order", () => {
  assert.deepEqual(
    memoryHitFields({ reason: "safe to reuse", similarity: 1, episode_id: "ep-405fdc2f96", zeta: null, verified: true }).map(([k]) => k),
    ["episode_id", "similarity", "verified", "reason", "zeta"],
  );
  assert.deepEqual(memoryHitFields({ zeta: null })[0], ["zeta", NOT_RECORDED]);
  assert.deepEqual(toolCallRows({ controller: 1, telemetry_investigator: 6, infrastructure_investigator: 10 }), [
    ["infrastructure_investigator", 10],
    ["telemetry_investigator", 6],
    ["controller", 1],
  ]);
});

// --- commands ---------------------------------------------------------------------------------------------

test("the terminal commands are the documented ones, with the free-plan consent flag and no secret", () => {
  const real = CLI_COMMANDS.real_llm.map((c) => c.command);
  assert.ok(real.some((c) => c.includes("evals/plans/m3-groq-free.yaml") && c.endsWith("--confirm-free-plan")));
  assert.ok(real.some((c) => c.includes("--from-data")));
  assert.ok(CLI_COMMANDS.scripted_regression.some((c) => c.command === ".venv/bin/python scripts/evaluate.py --no-latest"));
  assert.ok(CLI_COMMANDS.offline_replay.every((c) => c.command.startsWith(".venv/bin/python scripts/eval_replay.py")));
  const cluster = CLI_COMMANDS.real_kubernetes.map((c) => c.command);
  assert.ok(cluster.some((c) => c.includes("scripts/demo.py") && c.includes("--approve ask")), "a person approves each run's fix");
  assert.ok(cluster.some((c) => c.startsWith("RECAP_TARGET=kind-demo ") && c.includes("--from-data")));
  // Both clusters: SREGym's runs, its own grader, and its batch with the grader's verdicts.
  assert.ok(cluster.some((c) => c.includes("RECAP_TARGET=sregym-hotel") && c.includes("--scenario sregym-misconfig-app-hotel-res") && c.includes("--approve ask")));
  assert.ok(cluster.some((c) => c.includes("sregym.sh grade misconfig_app_hotel_res")));
  const sregymBatch = cluster.find((c) => c.startsWith("RECAP_TARGET=sregym-hotel ") && c.includes("--from-data"));
  assert.ok(sregymBatch);
  assert.match(sregymBatch, /--benchmark "SREGym 1\.1 mitigation oracle \(run-oracle\.py\), recorded by hand"/);
  assert.match(sregymBatch, /--benchmark-verdict RUN_ID=pass$/);
  assert.doesNotMatch(sregymBatch, /\|/, "no shell pipe in a command meant to be copied");
  assert.match(CLI_COMMANDS.real_kubernetes.find((c) => c.command === sregymBatch)?.note ?? "", /no verdict is ever merged into RECAP's own outcome/);
  const all = Object.values(CLI_COMMANDS).flat();
  for (const c of all) {
    assert.doesNotMatch(c.command, /API_KEY=|gsk_|Bearer|token/i, c.command);
    assert.doesNotMatch(c.command, /\/Users\/|\/home\//, "no local folder");
  }
});
