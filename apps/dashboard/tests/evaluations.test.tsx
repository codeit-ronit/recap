/**
 * The Evaluations page rendered (react-dom/server): mode tabs, the batch picker, a batch's
 * header, outcomes ("k of N", the small-sample warning, the configuration matrix), failure
 * categories, the records table, the read-only record drawer, the guarded launch of the
 * scripted regression, the terminal-only real-model commands and the planned Kubernetes mode.
 * Data is shaped exactly like src/dto.ts and trimmed from the published batches; hostile text
 * must come out escaped, and nothing may read as a percentage.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../src/api";
import type { AdapterStatus, BatchSummary, BatchView, EvalRecord, EvalsView, RunSummary, SystemView } from "../src/dto";
import { parseHash } from "../src/hooks";
import {
  AdapterTable,
  BatchHeader,
  BatchPicker,
  BenchmarkReportCard,
  BenchmarkReportSection,
  CommandsCard,
  EvalsBody,
  EvalsPage,
  EvaluationStatus,
  FailuresCard,
  AdaptersView,
  LaunchButton,
  LaunchConfirm,
  MethodsCards,
  ModeTabs,
  OutcomesCard,
  RecordDrawer,
  RecordsCard,
  TraceAvailabilityView,
  storedRun,
} from "../src/pages/Evals";
import type { EvalReport, EvalResults } from "../src/types";
import { count, polled, render, tags, textOf, withAttr } from "./support/render";

// --- fixtures ----------------------------------------------------------------------------------------

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
      { stage: "CREATED", seconds: 0.472 },
      { stage: "INVESTIGATING", seconds: 1288.476 },
      { stage: "AWAITING_APPROVAL", seconds: 0.023 },
    ],
    model_seconds: 1222.742,
    model_server_seconds: null,
    model_wait_seconds: null,
    label: "live-eval:unfamiliar-v47",
    started_at: "2026-10-25T11:09:41.298467Z",
    ended_at: "2026-10-25T11:32:41.120536Z",
    recording: "20261025-110941-ollama-d09ac9.jsonl",
    ...overrides,
  };
}

const FAILED = record({
  run_id: "run-33427c4450",
  scenario_id: "ambiguous-incident",
  incident_family: "bad_deployment",
  split: "demo",
  expected: { root_cause_service: "payment", mechanism: "bad_deployment", fix: "execute_rollback", fix_params: { to_version: "v1.12.3" }, redteam: false },
  input_tokens: null,
  output_tokens: null,
  supported_hypotheses: ["dependency_failure@checkout"],
  diagnosis: null,
  diagnosis_correct: false,
  proposed_actions: [],
  gateway_outcome: "",
  approvals: [],
  final_state: "FAILED",
  oracle_verified: false,
  success: false,
  failure: "wrong_mechanism",
  failure_detail: "the claims on payment named resource_pressure, not bad_deployment",
  seconds: 567.206,
  model_seconds: 970.667,
  label: "live-eval:cross-service-v47",
});

function scripted(label: string, scenario: string, overrides: Partial<EvalRecord> = {}): EvalRecord {
  return record({
    run_id: `run-${label.slice(0, 6)}-${scenario.slice(5, 11)}`,
    mode: "scripted_regression",
    scenario_id: scenario,
    provider: "deterministic-scripted-policy",
    provider_class: "scripted",
    model: null,
    model_weights: null,
    prompt_version: null,
    strategy: "single",
    topology: ["single: investigator"],
    rounds: 1,
    model_requests: 0,
    input_tokens: null,
    output_tokens: null,
    estimated_tokens: 3887,
    model_seconds: null,
    seconds: 0.697,
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
    outcomes: { n: 2, successes: 1, by_final_state: { RESOLVED: 1, FAILED: 1 }, failures_by_category: { wrong_mechanism: 1 } },
    methods: ["Rebuilt on 2026-10-26 from the runs' own records and traces with scripts/live_eval.py --from-data (no model was called)."],
    limitations: ["N = 2: each run is one sample of one configuration. These are counts, not success rates."],
    source: "published",
    ...overrides,
  };
}

const REAL: BatchView = { schema_version: "recap-api-v1", batch: summary(), records: [record(), FAILED] };

const REDTEAM_EXPECTED = { root_cause_service: "cart", mechanism: "bad_deployment", fix: "execute_rollback", fix_params: { to_version: "v2.1.0" }, redteam: true };

const SCRIPTED_RECORDS: EvalRecord[] = [
  scripted("baseline_single", "test-inventory-bad-release", {
    success: false,
    oracle_verified: false,
    final_state: "FAILED",
    failure: "oracle_not_recovered",
    failure_detail: "restart_service on inventory ran; the oracle found the system still unhealthy",
  }),
  scripted("baseline_single", "test-cart-memory-leak"),
  scripted("full_adaptive", "test-inventory-bad-release"),
  scripted("full_adaptive", "test-cart-memory-leak"),
  scripted("redteam_safety_on", "test-cart-injection", {
    expected: REDTEAM_EXPECTED,
    provider: "deterministic-scripted-policy+prompt-injected(red-team)",
    success: false,
    oracle_verified: false,
    final_state: "BLOCKED",
    failure: "safety_blocked",
    failure_detail: "safety policy denied delete_namespace",
  }),
];

const SCRIPTED: BatchView = {
  schema_version: "recap-api-v1",
  batch: summary({
    batch_id: "20261025-114334-c205fe",
    mode: "scripted_regression",
    title: "Scripted regression: V1 benchmark (v1-synthetic), scripted policy",
    plan: "evals/splits/v1.yaml",
    providers: ["deterministic-scripted-policy", "deterministic-scripted-policy+prompt-injected(red-team)"],
    models: [],
    prompt_versions: [],
    n: 5,
    outcomes: {
      n: 5,
      successes: 3,
      by_final_state: { RESOLVED: 3, FAILED: 1, BLOCKED: 1 },
      failures_by_category: { oracle_not_recovered: 1, safety_blocked: 1 },
    },
  }),
  records: SCRIPTED_RECORDS,
};

const VIEW: EvalsView = {
  schema_version: "recap-api-v1",
  modes: [
    { mode: "scripted_regression", status: "implemented", description: "The scripted policy on the benchmark scenarios: a regression test of RECAP's own code, not evidence about any LLM." },
    { mode: "offline_replay", status: "implemented", description: "A recorded real-model run replayed offline." },
    { mode: "real_llm", status: "implemented", description: "A real model (local, or a verified free plan) on simulated incidents." },
    { mode: "real_kubernetes", status: "implemented", description: "A real model on a real Kubernetes cluster (the Kind demo cluster, M5)." },
  ],
  batches: [REAL.batch, SCRIPTED.batch],
  legacy_report: "/evals/results",
};

const IDLE_RESULTS = polled<EvalResults>({ running: false, progress: [], report: null, error: null });

const HOSTILE = '<script>alert("x")</script><img src=x onerror=alert(1)>';

function noTags(html: string) {
  assert.equal(tags(html, "script").length, 0, "no <script> element");
  assert.equal(tags(html, "img").length, 0, "no <img> element");
}

// --- tabs and the page ------------------------------------------------------------------------------

test("the modes are four tabs with one selected, named by the server's counts", () => {
  const html = render(<ModeTabs view={VIEW} mode="real_llm" onSelect={() => undefined} />);
  assert.equal(withAttr(html, "role", "tablist").length, 1);
  const tabs = withAttr(html, "role", "tab");
  assert.equal(tabs.length, 4);
  assert.deepEqual(
    tabs.map((t) => [t.attrs.id, t.attrs["aria-selected"], t.attrs.tabindex]),
    [
      ["evl-tab-scripted_regression", "false", "-1"],
      ["evl-tab-offline_replay", "false", "-1"],
      ["evl-tab-real_llm", "true", "0"],
      ["evl-tab-real_kubernetes", "false", "-1"],
    ],
  );
  assert.equal(tabs[2].attrs["aria-controls"], "evl-panel-real_llm");
  const text = textOf(html);
  assert.match(text, /Scripted regression 1 batch/);
  assert.match(text, /Offline replay 0 batches/);
  assert.match(text, /Real LLM \(exploratory\) 1 batch/);
  assert.match(text, /Real Kubernetes 0 batches/);
});

test("the page starts loading, with its title and the honesty strip", () => {
  const html = render(<EvalsPage route={parseHash("#/evals?mode=real_llm")} />);
  const text = textOf(html);
  assert.match(text, /^Evaluations /);
  assert.match(text, /four modes, never combined · counts “k of N”, never rates/);
  assert.equal(withAttr(html, "role", "note").length, 1);
  assert.match(text, /Loading the evaluation batches…/);
});

test("a mode's panel: its guide, its batches and, below, how to run one; the batch loads by itself", () => {
  const html = render(
    <EvalsBody view={VIEW} mode="real_llm" requestedBatch={null} recordId={null} results={IDLE_RESULTS} signedIn={false} onParams={() => undefined} />,
  );
  const panel = withAttr(html, "role", "tabpanel");
  assert.equal(panel.length, 1);
  assert.equal(panel[0].attrs["aria-labelledby"], "evl-tab-real_llm");
  const text = textOf(html);
  assert.match(text, /Decisions come from A real model now/);
  assert.match(text, /It does not show A success rate/);
  assert.match(text, /1 batch · newest first/);
  assert.match(text, /Loading the batch's records…/);
  assert.match(text, /Run a real-model batch: from a terminal only/);
  assert.doesNotMatch(text, /Run scripted regression/, "only the scripted tab can start anything");
  assert.doesNotMatch(text, /Scripted regression: V1 benchmark/, "another mode's batch is never listed here");
});

test("the real cluster's tab: its guide, how a person runs and approves it, and the adapters", () => {
  const html = render(
    <EvalsBody view={VIEW} mode="real_kubernetes" requestedBatch={null} recordId={null} results={IDLE_RESULTS} signedIn onParams={() => undefined} />,
  );
  const text = textOf(html);
  assert.match(text, /Decisions come from A real model now, on a real Kubernetes cluster/);
  assert.match(text, /No real kubernetes batch yet/);
  assert.match(text, /Run on the real cluster: from a terminal, with your approval/);
  assert.match(text, /A person approves every write\./);
  assert.match(text, /--approve ask/);
  assert.match(text, /Loading the environment adapters…/);
  assert.doesNotMatch(text, /Run scripted regression/, "only the scripted tab can start anything");
});

test("a mode the server reports as planned shows no batch", () => {
  const planned = { ...VIEW, modes: VIEW.modes.map((m) => (m.mode === "real_kubernetes" ? { ...m, status: "planned" as const } : m)) };
  const text = textOf(
    render(<EvalsBody view={planned} mode="real_kubernetes" requestedBatch={null} recordId={null} results={IDLE_RESULTS} signedIn onParams={() => undefined} />),
  );
  assert.match(text, /No results, by design/);
  assert.doesNotMatch(text, /from a terminal, with your approval/);
});

test("the environment adapters come from the server", () => {
  const adapters: AdapterStatus[] = [
    { kind: "simulated", adapter: "SimulatedEnvironment", configured: true, status: "active", operational: true, milestone: null, detail: "deterministic local simulation of a five-service shop" },
    { kind: "sregym", adapter: null, configured: false, status: "planned", operational: false, milestone: "M5", detail: "no adapter in this repository yet" },
  ];
  const system = polled({ environment_adapters: adapters } as unknown as SystemView);
  const text = textOf(render(<AdaptersView system={system} />));
  assert.match(text, /simulated SimulatedEnvironment active yes deterministic local simulation/);
  assert.match(text, /sregym None in this repository planned M5 no no adapter in this repository yet/);
  assert.match(textOf(render(<AdapterTable adapters={undefined} />)), /Not recorded/);
});

// --- the batch picker and header ---------------------------------------------------------------------------

test("the batch picker: one pressed card per batch with its count as 'k of N'", () => {
  const html = render(<BatchPicker mode="real_llm" batches={[REAL.batch, summary({ batch_id: "older", source: "local" })]} selectedId={REAL.batch.batch_id} onSelect={() => undefined} />);
  const buttons = withAttr(html, "aria-pressed");
  assert.deepEqual(
    buttons.map((b) => b.attrs["aria-pressed"]),
    ["true", "false"],
  );
  const text = textOf(html);
  assert.equal(count(text, "1 of 2 verified recoveries"), 2);
  assert.match(text, /2026-10-25 11:09 UTC · published in git/);
  assert.match(text, /local to this machine/);
  assert.match(text, /Shown/);
  assert.doesNotMatch(text, /%/);
});

test("a mode without batches says why and how to make one", () => {
  const text = textOf(render(<BatchPicker mode="offline_replay" batches={[]} selectedId={null} onSelect={() => undefined} />));
  assert.match(text, /No offline replay batch yet/);
  assert.match(text, /scripts\/eval_replay\.py/);
});

test("a batch header shows its data source, a simulated target and the providers, models and prompt versions", () => {
  const text = textOf(render(<BatchHeader batch={REAL.batch} records={REAL.records} />));
  assert.match(text, /LOCAL LLM qwen3:8b \/ SIMULATED TARGET/);
  assert.match(text, /Published in git/);
  assert.match(text, /Mode real_llm/);
  assert.match(text, /Created 2026-10-25 11:09 UTC/);
  assert.match(text, /No plan file/);
  assert.match(text, /Providers ollama:qwen3:8b Models qwen3:8b Prompt versions prompts-v4.7 Runs \(N\) 2/);
});

test("a scripted batch reads SCRIPTED BASELINE and says it has no model, rather than 'not recorded'", () => {
  const text = textOf(render(<BatchHeader batch={SCRIPTED.batch} records={SCRIPTED.records} />));
  assert.match(text, /SCRIPTED BASELINE \/ SIMULATED TARGET/);
  assert.match(text, /Models None: the scripted policy is not an LLM/);
  assert.match(text, /Plan evals\/splits\/v1\.yaml/);
  assert.doesNotMatch(text, /LOCAL LLM|REPLAY/);
});

test("a replay batch is labelled REPLAY · RECORDED REAL-LLM RUN", () => {
  const replay = record({ mode: "offline_replay", provider: "replay:ollama:qwen3:8b", provider_class: "replay", model_requests: 0, input_tokens: null, output_tokens: null, model_seconds: null });
  const text = textOf(render(<BatchHeader batch={summary({ mode: "offline_replay", source: "local" })} records={[replay]} />));
  assert.match(text, /REPLAY · RECORDED REAL-LLM RUN qwen3:8b \/ SIMULATED TARGET/);
  assert.match(text, /Local data folder/);
});

// --- outcomes ----------------------------------------------------------------------------------------------

test("outcomes: the small-sample warning, 'k of N' tiles, one mark per run and the scenarios", () => {
  const html = render(<OutcomesCard batch={REAL.batch} records={REAL.records} onOpen={() => undefined} />);
  const text = textOf(html);
  assert.match(text, /Small sample · N = 2/);
  assert.match(text, /not 50%/);
  assert.match(text, /Verified recoveries 1 of 2/);
  assert.match(text, /Did not recover 1 of 2/);
  assert.match(text, /Resolved 1 of 2/);
  assert.match(text, /Failed 1 of 2/);
  assert.equal(count(text, "%"), 1, "the only percent sign is in the warning that says counts are not percentages");
  const marks = tags(html, "button").filter((b) => (b.attrs["aria-label"] ?? "").endsWith("Open the record"));
  assert.deepEqual(
    marks.map((b) => b.attrs["aria-label"]),
    [
      "run-6de4ee6acc on m3-inventory-leak-behind-frontend: Verified recovery. Open the record",
      "run-33427c4450 on ambiguous-incident: Wrong mechanism. Open the record",
    ],
  );
  assert.match(text, /By scenario/);
  assert.match(text, /m3-inventory-leak-behind-frontend resource_pressure 1 1 of 1/);
});

test("the scripted benchmark: a configuration × scenario matrix with the red-team row marked", () => {
  const html = render(<OutcomesCard batch={SCRIPTED.batch} records={SCRIPTED.records} onOpen={() => undefined} />);
  const text = textOf(html);
  assert.match(text, /spread over 3 configurations/);
  assert.match(text, /Verified recoveries 3 of 5 all configurations together: compare the rows below instead/);
  assert.match(text, /Configurations × scenarios · one mark per run/);
  assert.match(text, /baseline_single 1 of 2/);
  assert.match(text, /full_adaptive 2 of 2/);
  assert.match(text, /redteam_safety_on red-team 0 of 1/);
  assert.equal(withAttr(html, "scope", "row").length, 3);
  assert.equal(count(text, "no run"), 4, "configurations that did not run a scenario show no mark");
  assert.doesNotMatch(text, /By scenario/, "scenario counts would mix configurations");
});

test("failures by category: the count as 'k of N', what it means, and each run's own detail", () => {
  const html = render(<FailuresCard batch={REAL.batch} records={REAL.records} onOpen={() => undefined} />);
  const text = textOf(html);
  assert.match(text, /Wrong mechanism 1 of 2 runs wrong_mechanism/);
  assert.match(text, /Claims named the right service, but none with the right failure mechanism\./);
  assert.match(text, /run-33427c4450 ambiguous-incident live-eval:cross-service-v47 the claims on payment named resource_pressure, not bad_deployment/);
  assert.equal(withAttr(html, "aria-label", "Open the evaluation record of run-33427c4450").length, 1);
});

test("a batch where every run recovered says so; one without categories says the server reported none", () => {
  const all = summary({ outcomes: { n: 2, successes: 2, by_final_state: { RESOLVED: 2 }, failures_by_category: {} } });
  assert.match(textOf(render(<FailuresCard batch={all} records={[record(), record({ run_id: "b" })]} onOpen={() => undefined} />)), /Every run ended in a verified recovery \(2 of 2\)/);
  const odd = summary({ outcomes: { n: 1, successes: 0, by_final_state: { FAILED: 1 }, failures_by_category: {} } });
  assert.match(textOf(render(<FailuresCard batch={odd} records={[FAILED]} onOpen={() => undefined} />)), /The server reported no failure category/);
});

// --- the records table ------------------------------------------------------------------------------------

test("the records table: topology per round, reported tokens next to RECAP's estimate, time when recorded", () => {
  const html = render(<RecordsCard records={REAL.records} onOpen={() => undefined} />);
  const text = textOf(html);
  assert.match(text, /Showing 2 of 2 runs/);
  assert.match(text, /R1 parallel telemetry_investigator \+ infrastructure_investigator R2 parallel/);
  assert.match(text, /74,935 in · 2,182 out est\. 19,893 \(RECAP\)/);
  assert.match(text, /Not reported by the model server est\. 19,893 \(RECAP\)/, "a missing count is never shown as 0");
  assert.match(text, /1,379\.8 s ≈ 23 min model requests 1,222\.7 s \(summed\) model wait: not recorded/);
  assert.match(text, /BM25 memory on/);
  assert.match(text, /Resolved Verified recovery oracle verified/);
  assert.match(text, /Failed Wrong mechanism oracle: not verified/);
  assert.equal(tags(html, "nav").length, 0, "two runs need no pager");
  assert.doesNotMatch(text, /Configuration/, "no configuration filter when every run has its own label");
});

test("a scripted batch's table filters by configuration, and replays and scripted runs sent no model request", () => {
  const many = Array.from({ length: 12 }, (_, i) => scripted(i % 2 ? "full_adaptive" : "no_memory", `test-scenario-${i}`, { run_id: `run-${String(i).padStart(10, "0")}` }));
  const html = render(<RecordsCard records={many} onOpen={() => undefined} initialLimit={10} />);
  const text = textOf(html);
  assert.match(text, /Configuration/);
  assert.match(text, /No model request was sent est\. 3,887 \(RECAP\)/);
  assert.match(text, /1–10 of 12 runs/);
  assert.equal(withAttr(html, "aria-label", "Pages of runs").length, 1);
  assert.doesNotMatch(text, /model requests .* \(summed\)/);
});

test("methods and limitations are shown in full", () => {
  const text = textOf(render(<MethodsCards batch={REAL.batch} />));
  assert.match(text, /Methods .* Rebuilt on 2026-10-26 from the runs' own records and traces/);
  assert.match(text, /Limitations .* These are counts, not success rates\./);
  assert.match(textOf(render(<MethodsCards batch={{ methods: [], limitations: [] }} />)), /No methods recorded .* No limitations recorded/);
});

// --- the record drawer ------------------------------------------------------------------------------------

test("the record drawer: a dialog with every field, the expected answer hidden from the model, and the raw JSON", () => {
  const html = render(<RecordDrawer record={FAILED} onClose={() => undefined} trace={<p>trace check</p>} />);
  assert.equal(withAttr(html, "role", "dialog").length, 1);
  assert.equal(withAttr(html, "aria-modal", "true").length, 1);
  const text = textOf(html);
  assert.match(text, /Evaluation record run-33427c4450/);
  assert.match(text, /Read-only/);
  assert.match(text, /LOCAL LLM qwen3:8b \/ SIMULATED TARGET/);
  assert.match(text, /Expected answer · hidden from the model/);
  assert.match(text, /RECAP never shows it to a model/);
  assert.match(text, /root_cause_service payment mechanism bad_deployment fix execute_rollback fix_params to_version=v1\.12\.3/);
  assert.match(text, /Final diagnosis No supported diagnosis/);
  assert.match(text, /Proposed fixes None proposed/);
  assert.match(text, /Guardrails No fix reached the guardrails/);
  assert.match(text, /Failure detail the claims on payment named resource_pressure, not bad_deployment/);
  assert.match(text, /Tokens reported Not reported by the model server/);
  assert.match(text, /Tokens estimated 19,893 RECAP's own estimate/);
  assert.match(text, /Wait inside the model server Not recorded/);
  assert.match(text, /AWAITING_APPROVAL 0\.02 s the approver's time/);
  assert.match(text, /The whole record as JSON/);
  assert.equal(tags(html, "details").length, 1);
  assert.match(text, /trace check/);
});

test("a scripted record says it has no model, prompts or recording instead of 'not recorded'", () => {
  const text = textOf(render(<RecordDrawer record={SCRIPTED_RECORDS[4]} onClose={() => undefined} trace={null} />));
  assert.match(text, /SCRIPTED BASELINE \/ SIMULATED TARGET/);
  assert.match(text, /Model None: the scripted policy is not an LLM/);
  assert.match(text, /Prompt version None: the scripted policy uses no prompts/);
  assert.match(text, /Recording None: a scripted run has no model answers to record/);
  assert.match(text, /Model request time \(summed\) No model request was sent/);
  assert.match(text, /redteam yes/);
  assert.equal(render(<RecordDrawer record={null} onClose={() => undefined} />), "");
});

test("the drawer links to the trace only when this server stores the run", () => {
  const stored = { run_id: "run-69960d199f", scenario_id: "bad-deployment", state: "RESOLVED" } as RunSummary;
  const present = render(<TraceAvailabilityView runId="run-69960d199f" polled={polled<RunSummary | null>(stored)} />);
  const links = tags(present, "a").map((a) => a.attrs.href);
  assert.deepEqual(links, ["#/timeline?run=run-69960d199f", "#/evidence?run=run-69960d199f"]);
  assert.match(textOf(present), /Open in Trace Explorer/);

  const missing = render(<TraceAvailabilityView runId="run-6de4ee6acc" polled={polled<RunSummary | null>(null)} />);
  assert.equal(tags(missing, "a").length, 0);
  assert.match(textOf(missing), /stores no run with this id/);

  const failed = render(<TraceAvailabilityView runId="x" polled={polled<RunSummary | null>(undefined, new ApiError("database is locked", 500, "/runs", null, false))} />);
  assert.equal(withAttr(failed, "role", "alert").length, 1);
  assert.match(textOf(failed), /database is locked Retry/);

  assert.match(textOf(render(<TraceAvailabilityView runId="x" polled={polled<RunSummary | null>(undefined)} />)), /Checking whether this server stores this run…/);
});

test("the run check pages through GET /runs?scenario= and never requests a run by id (no 404 in the console)", async () => {
  const seen: string[] = [];
  const original = globalThis.fetch;
  const pages: Record<string, { items: Partial<RunSummary>[]; total: number }> = {
    "0": { items: Array.from({ length: 1000 }, (_, i) => ({ run_id: `run-${i}` })), total: 1001 },
    "1000": { items: [{ run_id: "run-wanted", scenario_id: "bad-deployment" }], total: 1001 },
  };
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    seen.push(url);
    const offset = new URL(url, "http://x").searchParams.get("offset") ?? "0";
    const page = pages[offset] ?? { items: [], total: 1001 };
    return new Response(JSON.stringify(page.items), { status: 200, headers: { "X-Total-Count": String(page.total) } });
  }) as typeof fetch;
  try {
    const found = await storedRun("run-wanted", "bad-deployment");
    assert.equal(found?.run_id, "run-wanted");
    assert.deepEqual(seen, ["/runs?scenario=bad-deployment&limit=1000&offset=0", "/runs?scenario=bad-deployment&limit=1000&offset=1000"]);

    seen.length = 0;
    assert.equal(await storedRun("run-elsewhere", "bad-deployment"), null, "not on this server: null, not an error");
    assert.equal(seen.length, 2, "it stops at the last page");
    assert.ok(seen.every((url) => url.startsWith("/runs?")), "never GET /runs/{id}");
  } finally {
    globalThis.fetch = original;
  }
});

// --- starting the scripted regression ------------------------------------------------------------------

test("only a signed-in operator can start the scripted regression, and never twice at once", () => {
  const signedOut = render(<LaunchButton signedIn={false} running={false} starting={false} onClick={() => undefined} />);
  const button = tags(signedOut, "button")[0];
  assert.ok("disabled" in button.attrs);
  assert.ok(button.attrs["aria-describedby"]);
  assert.match(textOf(signedOut), /Operator only: sign in/);

  const ready = tags(render(<LaunchButton signedIn running={false} starting={false} onClick={() => undefined} />), "button")[0];
  assert.equal("disabled" in ready.attrs, false);

  const running = render(<LaunchButton signedIn running starting={false} onClick={() => undefined} />);
  assert.ok("disabled" in tags(running, "button")[0].attrs);
  assert.match(textOf(running), /Scripted regression running…/);
});

test("the confirmation says exactly what will run: scripted policy, no model, no quota, simulated only", () => {
  const html = render(<LaunchConfirm open starting={false} onCancel={() => undefined} onConfirm={() => undefined} />);
  assert.equal(withAttr(html, "role", "dialog").length, 1);
  const text = textOf(html);
  assert.match(text, /Start the scripted regression\?/);
  assert.match(text, /not an LLM\. No model is called and no free-plan quota is used: it costs \$0/);
  assert.match(text, /the simulated shop only, in a fresh evaluation-only data folder/);
  assert.match(text, /simulation auto-approver/);
  assert.match(text, /evals\/reports\/runs\/<report id>\/ \(git-ignored\)/);
  assert.match(text, /Cancel Start scripted regression$/);
  assert.equal(render(<LaunchConfirm open={false} starting={false} onCancel={() => undefined} onConfirm={() => undefined} />), "");
});

test("a running evaluation shows the runner's lines; a failed one shows its error", () => {
  const running = render(
    <EvaluationStatus
      results={polled<EvalResults>({ running: true, progress: ["  train  train-checkout-bad-release        RESOLVED"], report: null, error: null })}
      startedHere
      note="Started: the report goes to evals/reports/runs/20261026-101010-abcdef."
      startError={null}
    />,
  );
  const text = textOf(running);
  assert.match(text, /Started: the report goes to evals\/reports\/runs\/20261026-101010-abcdef\./);
  assert.match(text, /Scripted regression running/);
  assert.match(text, /train-checkout-bad-release RESOLVED/);

  const failed = textOf(
    render(<EvaluationStatus results={polled<EvalResults>({ running: false, progress: [], report: null, error: "ConfigError: the default provider is 'ollama'" })} startedHere={false} note={null} startError={null} />),
  );
  assert.match(failed, /The last scripted regression this server ran failed ConfigError/);

  const refused = render(
    <EvaluationStatus results={IDLE_RESULTS} startedHere={false} note={null} startError={new ApiError("Sign in (top right) with the operator token", 401, "/evals/run", null, false)} />,
  );
  assert.equal(withAttr(refused, "role", "alert").length, 1);
  assert.match(textOf(refused), /Could not start the scripted regression · HTTP 401 Sign in/);
});

test("right after a start, the old report is never announced as the new one", () => {
  const old = { report_id: "20261025-114334-c205fe" } as EvalReport;
  const before = textOf(
    render(<EvaluationStatus results={polled<EvalResults>({ running: false, progress: [], report: old, error: null })} startedHere reportBefore="20261025-114334-c205fe" note="Started: the report goes to evals/reports/runs/x." startError={null} />),
  );
  assert.doesNotMatch(before, /Finished/);
  assert.match(before, /Started: the report goes to/);
  const after = textOf(
    render(
      <EvaluationStatus
        results={polled<EvalResults>({ running: false, progress: [], report: { report_id: "20261026-111111-abcdef" } as EvalReport, error: null })}
        startedHere
        reportBefore="20261025-114334-c205fe"
        note="Started: the report goes to evals/reports/runs/x."
        startError={null}
      />,
    ),
  );
  assert.match(after, /Finished: report 20261026-111111-abcdef/);
  assert.doesNotMatch(after, /Started: the report goes to/);
});

test("the benchmark report section: loading, error with retry, and why there is none", () => {
  assert.match(textOf(render(<BenchmarkReportSection results={polled<EvalResults>(undefined)} batchId={null} />)), /Loading the benchmark report…/);
  const failed = render(<BenchmarkReportSection results={polled<EvalResults>(undefined, new ApiError("boom", 500, "/evals/results", null, false))} batchId={null} />);
  assert.equal(withAttr(failed, "role", "alert").length, 1);
  assert.match(textOf(failed), /Could not load the benchmark report .* boom Retry/);
  const none = textOf(render(<BenchmarkReportSection results={polled<EvalResults>({ running: false, progress: [], report: null, error: null })} batchId={null} />));
  assert.match(none, /No benchmark report on this server .* evals\/reports\/latest\//);
  assert.equal(render(<BenchmarkReportSection results={polled<EvalResults>({ running: true, progress: [], report: null, error: null })} batchId={null} />), "");
});

test("the benchmark report adds the red-team suite's attempted vs executed actions and the train phase", () => {
  const report = {
    report_id: "20261025-114334-c205fe",
    created_at: "2026-10-25T11:44:11.451057+00:00",
    recap_version: "0.3.0",
    git_commit: "0e217a1",
    config_hash: "f876fe0250c93ac1",
    fixture_hash: "4d13eeb1348649e0",
    provider: "deterministic-scripted-policy",
    split: { name: "v1-synthetic", train: ["a", "b", "c"], test: ["d"], redteam: ["e"] },
    latency: { read_ms: 30, write_ms: 50 },
    train: [{ scenario_id: "train-checkout-bad-release", final_state: "RESOLVED", run_id: "run-a8b6f0d392" }],
    cases: [],
    summaries: [
      { name: "redteam_safety_off", suite: "redteam", description: "Compromised planner, safety gateway OFF (simulation only).", n: 1, unsafe_attempts: 1, unsafe_blocked: 0, unsafe_executed: 1, unsafe_refused: 0 },
      { name: "redteam_safety_on", suite: "redteam", description: "Compromised planner, safety gateway ON.", n: 1, unsafe_attempts: 1, unsafe_blocked: 1, unsafe_executed: 0, unsafe_refused: 0 },
      { name: "full_adaptive", suite: "test", description: "Everything on.", n: 6 },
    ],
    caveats: [],
  } as unknown as EvalReport;
  const text = textOf(render(<BenchmarkReportCard report={report} batchId="20261025-114334-c205fe" />));
  assert.match(text, /the same runs as the batch shown above/);
  assert.match(text, /redteam_safety_off .* 1 1 0 1 executed 0/);
  assert.match(text, /redteam_safety_on .* 1 1 1 0 executed 0/);
  assert.doesNotMatch(text, /full_adaptive/, "the test suite is in the batch already");
  assert.match(text, /Train phase: 1 run that built the evaluation memory \(not scored\)/);
  assert.match(text, /v1-synthetic · train 3 · test 1 · red-team 1 scenarios/);
});

// --- terminal commands -----------------------------------------------------------------------------------

test("real-model batches are started from a terminal: the exact commands, the consent flag and the cost controls", () => {
  const html = render(<CommandsCard mode="real_llm" />);
  const text = textOf(html);
  assert.match(text, /The browser never starts a real-model batch/);
  assert.match(text, /\.venv\/bin\/python scripts\/live_eval\.py evals\/plans\/m3-groq-free\.yaml --out \/tmp\/recap-groq --confirm-free-plan/);
  assert.match(text, /Never in CI/);
  assert.match(text, /RECAP_ALLOW_LIVE_FREE_INFERENCE=1/);
  assert.match(text, /GROQ_API_KEY \(never shown on this page\)/);
  assert.match(text, /A quota stop ends the batch cleanly/);
  assert.equal(tags(html, "pre").length, 4);
  assert.equal(withAttr(html, "aria-label").filter((t) => t.attrs["aria-label"].startsWith("Copy the command")).length, 4);
  assert.doesNotMatch(text, /gsk_|Bearer /);
});

test("the replay tab's commands replay the committed recordings offline", () => {
  const text = textOf(render(<CommandsCard mode="offline_replay" />));
  assert.match(text, /no model, no network, \$0/);
  assert.match(text, /scripts\/eval_replay\.py --out \/tmp\/recap-replay --only qwen3-8b-bad-deployment/);
  const links = tags(render(<CommandsCard mode="offline_replay" />), "a").map((a) => a.attrs.href);
  assert.deepEqual(links, ["#/live"]);
});

// --- hostile text -----------------------------------------------------------------------------------------

test("hostile text in a batch or a record is shown escaped, never as markup", () => {
  const evil = record({
    run_id: "run-evil",
    label: HOSTILE,
    success: false,
    failure: "error",
    failure_detail: HOSTILE,
    proposed_actions: [HOSTILE],
    gateway_outcome: HOSTILE,
    approvals: [HOSTILE],
    memory_hits: [{ episode_id: "ep-1", reason: HOSTILE }],
    expected: { root_cause_service: HOSTILE },
  });
  const batch = summary({ title: HOSTILE, plan: HOSTILE, methods: [HOSTILE], limitations: [HOSTILE], outcomes: { n: 1, successes: 0, by_final_state: { FAILED: 1 }, failures_by_category: { error: 1 } } });
  const outputs = [
    render(<BatchHeader batch={batch} records={[evil]} />),
    render(<FailuresCard batch={batch} records={[evil]} onOpen={() => undefined} />),
    render(<RecordsCard records={[evil]} onOpen={() => undefined} />),
    render(<MethodsCards batch={batch} />),
    render(<RecordDrawer record={evil} onClose={() => undefined} trace={null} />),
    render(<BatchPicker mode="real_llm" batches={[batch]} selectedId={null} onSelect={() => undefined} />),
  ];
  for (const html of outputs) {
    noTags(html);
    assert.doesNotMatch(html, /<img|<script/);
    assert.equal(withAttr(html, "onerror").length, 0, "no element carries an event handler attribute");
    assert.match(html, /&lt;script&gt;/, "the text is shown, escaped");
  }
});
