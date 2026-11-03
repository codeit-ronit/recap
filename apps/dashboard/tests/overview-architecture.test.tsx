/**
 * The Overview and Architecture & Learning pages, rendered with server answers shaped exactly
 * like src/dto.ts (GET /system, /providers, /runs, /replays as a seeded server returned them):
 * readiness with Kind and SREGym planned, providers without keys, the newest runs with their
 * data source, outcomes counted per source (never one success rate), active incidents, access,
 * the M5 gaps, and the flow map (a vertical tab list whose statuses come from /system).
 * Also: loading / empty / error states, partial records ("Not recorded") and hostile text.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../src/api";
import type { Page } from "../src/api";
import type * as dto from "../src/dto";
import { parseHash } from "../src/hooks";
import { ArchitecturePage, ArchitectureView, Breakable } from "../src/pages/Architecture";
import { OverviewPage, OverviewView, RECENT_RUNS, demoFixture, demoHref, sessionState } from "../src/pages/Overview";
import type { OverviewViewProps } from "../src/pages/Overview";
import type { SessionState } from "../src/view/system";
import { count, polled, render, tags, textOf, withAttr } from "./support/render";

// --- fixtures (the seeded demo server's answers) ----------------------------------------------

function component(id: string, iface: string, implementation: string, status: dto.ComponentState, detail = ""): dto.ComponentStatus {
  return { id, interface: iface, implementation, status, detail };
}

const COMPONENTS: dto.ComponentStatus[] = [
  component("environment", "TargetEnvironment", "RECAP simulated shop (kind simulated)", "active", "17 scenarios; simulated"),
  component("tool_transport", "ToolTransport", "MCP over stdio (official Python SDK, subprocess per run)", "active", "agents reach the environment only through these tools, past the gateway"),
  component("provider", "LLMProvider", "deterministic-scripted-policy", "active", "the scripted baseline: not an LLM"),
  component("retriever", "RetrievalIndex", "BM25 keyword search with a lexical reranker (a heuristic, not a neural model)", "active", "42 runbook chunks; default search: bm25"),
  component("hybrid_retrieval", "Embedder", "ollama:nomic-embed-text (local, loopback) + reciprocal rank fusion", "available", "stored vectors cover every runbook chunk"),
  component("memory", "ExperienceStore", "SQLite episodes, consolidator v2 (deterministic extraction)", "active", "8 episodes in partitions train, test, demo, live"),
  component("router", "RouterPolicy", "rules-router-v1", "active", "single or parallel investigation, from routing signals and the budget"),
  component("safety_policy", "SafetyPolicy", "recap-policy-engine (policy-v1)", "active", "judges every tool call and every fix from trusted configuration"),
  component("approvals", "ApprovalStore", "exact, single-use approvals bound to a proposal digest", "active", "a rejection wins until the approval is used"),
  component("write_boundary", "TargetEnvironment.execute_authorized", "Ed25519-signed, single-use capabilities", "active", "the environment refuses any write the gateway did not sign"),
  component("verifier", "Verifier", "evidence-rule-verifier-v1", "active", "evidence rules decide which claims are supported; agents never vote"),
  component("oracle", "RecoveryOracle", "simulated-health-oracle", "active", "judges recovery from the environment's own health"),
  component("trace_sinks", "TraceSink", "sqlite, jsonl, live-stream", "active", "every run's events in SQLite and JSONL"),
  component("opentelemetry", "TraceSink", "OpenTelemetry console spans", "disabled", "optional: RECAP_ENABLE_OTEL=1 with the otel extra installed"),
];

const ADAPTERS: dto.AdapterStatus[] = [
  { kind: "simulated", adapter: "SimulatedEnvironment", configured: true, status: "active", operational: true, milestone: null, detail: "deterministic local simulation of a five-service shop (recap/environments/simulated/)" },
  { kind: "sregym", adapter: "SREGymEnvironment", configured: false, status: "planned", operational: false, milestone: "M5", detail: "interface only: every operation raises EnvironmentNotIntegrated (integrations/sregym/); the real integration is planned for M5" },
  { kind: "kind", adapter: null, configured: false, status: "planned", operational: false, milestone: "M5", detail: "no adapter in this repository: a local Kind cluster is the first M5 target" },
];

const SYSTEM: dto.SystemView = {
  schema_version: "recap-api-v1",
  environment: {
    kind: "simulated",
    name: "RECAP simulated shop",
    namespace: "shop",
    is_simulated: true,
    supported_actions: ["rollback", "restart", "delete_namespace"],
    description: "Deterministic local simulation of a five-service shop (state in a per-run SQLite file). No real cluster is involved.",
    stabilization_timeout_seconds: 0,
    stabilization_poll_seconds: 2,
  },
  environment_kind: "simulated",
  tool_transport: "MCP over stdio (official Python SDK, subprocess per run)",
  provider: { name: "deterministic-scripted-policy", kind: "scripted", origin: "scripted", model: null, description: "Scripted baseline policy (NOT an LLM)", red_team: false },
  providers_available: [],
  retriever: "bm25",
  retrieval_mode: "bm25",
  hybrid_ready: true,
  embedder: "ollama:nomic-embed-text",
  knowledge_chunks: 42,
  router: "rules-router-v1",
  safety_policy: "recap-policy-engine (policy-v1)",
  verifier: "evidence-rule-verifier-v1",
  oracle: "simulated-health-oracle",
  memory_episodes: 8,
  trace_sinks: ["sqlite", "jsonl", "live-stream"],
  config_hash: "394843bc06d11ea0",
  fixture_hash: "ba4c521624b4b774",
  code_version: "673bb8ad878a",
  label: "SIMULATED LOCAL ENVIRONMENT · V1",
  simulation_notice:
    "SIMULATED ENVIRONMENT: every incident runs against RECAP's local simulation of a five-service shop. No real cluster, Kubernetes or production system is touched.",
  components: COMPONENTS,
  environment_adapters: ADAPTERS,
  features: {
    retrieval_default: "bm25",
    hybrid_ready: true,
    embeddings_enabled: false,
    vector_index: { embedder: "ollama:nomic-embed-text", dimensions: 768, chunks: 42 },
    knowledge_chunks: 42,
    knowledge_overlays: [],
    agent_search: { max_per_run: 6, k: 3 },
    memory_partitions: ["train", "test", "demo", "live"],
    memory_access: {
      scripted: { read: ["train", "demo"], write: "demo" },
      local_llm: { read: ["train", "live"], write: "live" },
      free_hosted_llm: { read: ["train", "live"], write: "live" },
      replay: { read: ["train", "live"], write: null },
    },
    replay_fixtures: 2,
    replay_fixtures_launchable: 2,
    writes_require_operator_token: true,
    reads_require_token: false,
    per_run_providers: ["deterministic", "ollama", "replay"],
    otel: false,
    evaluation_modes: { scripted_regression: "implemented", offline_replay: "implemented", real_llm: "implemented", real_kubernetes: "implemented" },
  },
  versions: {
    recap: "0.4.0",
    api_schema: "recap-api-v1",
    trace_schema: "recap.v1",
    prompt_version: "prompts-v4.7",
    policy_version: "policy-v1",
    router: "rules-router-v1",
    verifier: "evidence-rule-verifier-v1",
    config_hash: "394843bc06d11ea0",
    fixture_hash: "ba4c521624b4b774",
    code_version: "673bb8ad878a",
    python: "3.11.15",
  },
};

const NO_LIMITS: dto.ProviderLimitsView = {
  max_output_tokens: null,
  context_tokens: null,
  request_timeout_seconds: null,
  max_attempts: null,
  run_budget_overrides: {},
  free_daily_request_limit: null,
  limits_checked_on: null,
  caps: null,
  reserved_output_tokens: null,
};

function provider(over: Partial<dto.ProviderView> & Pick<dto.ProviderView, "name" | "kind" | "origin">): dto.ProviderView {
  return {
    model: null,
    mode: null,
    description: "",
    default: false,
    status: "ready",
    detail: "",
    usage: null,
    base_url: null,
    think: null,
    context_tokens: null,
    installed: null,
    key_env: null,
    key_present: null,
    opt_in_env: null,
    opted_in: null,
    attestation_env: null,
    confirmed_on: null,
    confirmation_age_days: null,
    confirmation_fresh: null,
    allowed_models: null,
    free_daily_request_limit: null,
    limits_checked_on: null,
    caps: null,
    last_rate_limit: null,
    stopped: null,
    recording: null,
    replay_of: null,
    provider_class: over.origin,
    key_required: false,
    selectable: false,
    selection_rule: "",
    limits: NO_LIMITS,
    limitations: [],
    ...over,
  };
}

const PROVIDERS: dto.ProviderView[] = [
  provider({
    name: "anthropic",
    kind: "llm",
    origin: "paid_llm",
    model: "claude-opus-5-5",
    mode: "json",
    description: "Optional Claude adapter (official Anthropic SDK). PAID per call: disabled for this $0 project.",
    status: "disabled",
    detail: "paid API: off for this $0 project",
    key_required: true,
    selection_rule: "a paid API: never selectable from the API (this project's budget is $0)",
    limitations: ["A paid API billed per call: disabled for this $0 project and never selectable from the API."],
  }),
  provider({
    name: "groq",
    kind: "llm",
    origin: "free_hosted_llm",
    model: "openai/gpt-oss-120b",
    mode: "json",
    description: "Groq Free plan (no payment method) via its OpenAI-compatible API. Fails closed.",
    status: "needs_setup",
    detail: "set GROQ_API_KEY in the environment",
    usage: { requests_last_minute: 0, requests_last_24h: 0, tokens_last_24h: 0 },
    key_env: "GROQ_API_KEY",
    key_present: false,
    opt_in_env: "RECAP_ALLOW_LIVE_FREE_INFERENCE",
    opted_in: false,
    attestation_env: "RECAP_GROQ_FREE_PLAN_CONFIRMED_ON",
    confirmation_fresh: false,
    key_required: true,
    selection_rule: "chosen in server configuration only (api_selectable is off for it)",
    limitations: ["A hosted free plan: a quota stop defers the run (QUOTA_EXHAUSTED) instead of switching provider."],
  }),
  provider({
    name: "ollama",
    kind: "llm",
    origin: "local_llm",
    model: "qwen3:8b",
    mode: "tools",
    description: "Open-weight model served by Ollama on this machine. No network, no charge.",
    detail: "local model installed; $0",
    usage: { requests_last_minute: 0, requests_last_24h: 12, tokens_last_24h: 48210 },
    base_url: "http://127.0.0.1:11434",
    installed: ["nomic-embed-text:latest", "qwen3:8b"],
    selectable: true,
    selection_rule: "marked api_selectable in configs/providers.yaml by the owner",
    limitations: ["A single Ollama server may answer one request at a time, so parallel agents' requests can wait for each other inside it."],
  }),
  provider({
    name: "replay",
    kind: "replay",
    origin: "replay",
    description: "Replays a recorded real-LLM run offline (no network). Marked REPLAY, never live.",
    detail: "replays a recorded qwen3:8b run; no model is called",
    recording: "fixtures/recordings/ollama-qwen3-8b-bad-deployment.jsonl",
    replay_of: { original_provider: "ollama", model: "qwen3:8b", created_at: "2026-10-25T11:06:04.270663+00:00", prompt_version: "prompts-v4.7" },
    selectable: true,
    selection_rule: "only with replay_fixture, the id of a committed recording (GET /replays)",
    limitations: ["Replays the recorded answers of an earlier real-model run: no model is called now."],
  }),
  provider({
    name: "deterministic",
    kind: "scripted",
    origin: "scripted",
    description: "Scripted baseline policy (not an LLM): deterministic playbooks and heuristics.",
    default: true,
    detail: "scripted policy, not an LLM; $0",
    selectable: true,
    selection_rule: "the server's default provider",
    limitations: ["Not an LLM: fixed playbooks and heuristics written for the simulated incidents."],
  }),
];

function replayProvenance(fixture: string, recording: string, title: string): dto.ReplayProvenance {
  return {
    fixture_id: fixture,
    recording_id: recording,
    recording_matches_fixture: true,
    committed_fixture: true,
    original_provider: "ollama",
    model: "qwen3:8b",
    prompt_version: "prompts-v4.7",
    recorded_at: "2026-10-25T11:06:04.270663+00:00",
    fixture_title: title,
    fixture_sha256: "991c1192dd6d",
    original_export: null,
    originally_approved_by: "the simulation auto-approver",
  };
}

let seq = 0;
function summary(over: Partial<dto.RunSummary> = {}): dto.RunSummary {
  seq += 1;
  const started = `2026-10-26T05:01:${String(40 - seq).padStart(2, "0")}.494265Z`;
  return {
    run_id: `run-seed${String(seq).padStart(4, "0")}`,
    scenario_id: "bad-deployment",
    state: "RESOLVED",
    label: "seed-demo",
    created_at: started,
    updated_at: started,
    modes: ["single"],
    recovered: true,
    outcome_note: "oracle verified recovery (simulated environment)",
    provider: "deterministic-scripted-policy",
    tool_calls: 7,
    pending_approval: false,
    status: "finished",
    terminal: true,
    outcome: "resolved",
    provider_class: "scripted",
    provider_kind: "scripted",
    model: null,
    prompt_version: null,
    environment_kind: "simulated",
    environment_simulated: true,
    config_hash: "394843bc06d11ea0",
    code_version: "cdc5df8a7f2f",
    recap_version: "0.4.0",
    started_at: started,
    ended_at: started,
    wall_seconds: 0.826,
    rounds: 1,
    agents: ["investigator"],
    model_requests: 0,
    oracle: null,
    replay: null,
    export: null,
    recording: null,
    episode_id: null,
    error: null,
    labels: ["SIMULATED ENVIRONMENT", "SCRIPTED BASELINE (NOT AN LLM)"],
    not_recorded: [],
    ...over,
  };
}

function replayRun(over: Partial<dto.RunSummary>): dto.RunSummary {
  return summary({
    provider: "replay:ollama:qwen3:8b",
    provider_class: "replay",
    provider_kind: "replay",
    model: "qwen3:8b",
    prompt_version: "prompts-v4.7",
    labels: ["SIMULATED ENVIRONMENT", "REPLAY", "RECORDED REAL-LLM RUN"],
    ...over,
  });
}

/** The seeded folder: two replays of two recordings, then nine scripted runs (newest first). */
const RUNS: dto.RunSummary[] = [
  replayRun({ run_id: "run-69960d199f", wall_seconds: 0.842, replay: replayProvenance("qwen3-8b-bad-deployment", "20261025-110603-ollama-b1ae62", "Bad deployment, one investigator (CI fixture)") }),
  replayRun({
    run_id: "run-f9395a4471",
    scenario_id: "m3-inventory-leak-behind-frontend",
    modes: ["parallel", "parallel"],
    rounds: 2,
    agents: ["telemetry_investigator", "infrastructure_investigator"],
    replay: replayProvenance("qwen3-8b-unfamiliar-parallel", "20261025-121828-ollama-6a5cfd", "Unfamiliar incident, two specialists, owner-approved fix (M3 deliverable)"),
  }),
  summary({ run_id: "run-8880c22740", scenario_id: "m3-payment-release-under-forged-signals" }),
  summary({ run_id: "run-71836eed04", scenario_id: "m3-inventory-leak-behind-frontend" }),
  summary({ run_id: "run-a4f9926bef", scenario_id: "budget-exhausted", state: "BUDGET_EXHAUSTED", outcome: "budget_exhausted", recovered: false }),
  summary({ run_id: "run-6539891f82", scenario_id: "malicious-log", state: "BLOCKED", outcome: "safety_blocked", recovered: false }),
  summary({ run_id: "run-debd9efab6", scenario_id: "memory-caution" }),
  summary({ run_id: "run-54acb295ef", scenario_id: "ambiguous-incident" }),
  summary({ run_id: "run-03c9c7c6fb", scenario_id: "bad-deployment" }),
  summary({ run_id: "run-a8b6f0d392", scenario_id: "train-checkout-bad-release" }),
  summary({ run_id: "run-0ldest0001", scenario_id: "train-inventory-leak" }),
];

function fixture(over: Partial<dto.FixtureView> = {}): dto.FixtureView {
  return {
    id: "qwen3-8b-unfamiliar-parallel",
    title: "Unfamiliar incident, two specialists, owner-approved fix (M3 deliverable)",
    description: "qwen3:8b on the held-out m3-inventory-leak-behind-frontend incident: two specialists in parallel for two rounds; resource pressure on inventory supported; restart approved by the owner; oracle verified.",
    scenario: "m3-inventory-leak-behind-frontend",
    strategy: "parallel",
    use_memory: true,
    retrieval: "bm25",
    provider: "ollama",
    model: "qwen3:8b",
    prompt_version: "prompts-v4.7",
    agents: "two specialists in parallel (telemetry, infrastructure), two rounds",
    approved_by: "the owner, at an interactive terminal (162.5 s after the request)",
    exported_run: "evals/live/2026-10-25-qwen3-8b-human-approved-unfamiliar-prompts-v4.7/",
    tags: ["multi-agent", "held-out", "human-approved", "golden"],
    recording_id: "20261025-121828-ollama-6a5cfd",
    file: "ollama-qwen3-8b-unfamiliar-parallel.jsonl",
    bytes: 225578,
    sha256: "37830779420b",
    integrity: "intact",
    prompt_version_matches: true,
    scenario_available: true,
    launchable: true,
    not_launchable_because: [],
    labels: ["RECORDED REAL-LLM RUN", "SIMULATED TARGET"],
    ...over,
  };
}

const REPLAYS: dto.ReplaysView = {
  schema_version: "recap-api-v1",
  current_prompt_version: "prompts-v4.7",
  fixtures: [
    fixture({ id: "qwen3-8b-bad-deployment", title: "Bad deployment, one investigator (CI fixture)", tags: ["single-agent"], exported_run: null, approved_by: "the simulation auto-approver" }),
    fixture(),
  ],
  manifest_error: null,
  how_to_launch: "POST /incidents/run (operator) with {scenario_id, config: {provider: 'replay', replay_fixture: <id>}}",
};

const NOW = Date.parse("2026-10-26T05:30:00Z");

function page(items: dto.RunSummary[], total: number | null = items.length): Page<dto.RunSummary> {
  return { items, total };
}

function overview(over: Partial<OverviewViewProps> = {}): string {
  const props: OverviewViewProps = {
    system: polled(SYSTEM),
    providers: polled(PROVIDERS),
    runs: polled(page(RUNS)),
    replays: polled(REPLAYS),
    session: { kind: "signed_out" },
    now: NOW,
    ...over,
  };
  return render(<OverviewView {...props} />);
}

/** The markup of the first element whose opening tag carries `marker`, up to the next sibling marker. */
function between(html: string, start: string, end: string): string {
  const from = html.indexOf(start);
  assert.ok(from >= 0, `missing ${start}`);
  const to = html.indexOf(end, from + start.length);
  return html.slice(from, to === -1 ? undefined : to);
}

const hrefs = (html: string) => tags(html, "a").map((a) => a.attrs.href ?? "");

// --- Overview ---------------------------------------------------------------------------------

test("before the first answer every card shows its loading state", () => {
  const html = render(<OverviewPage route={parseHash("#/overview")} system={polled(undefined)} />);
  const text = textOf(html);
  assert.match(text, /Overview/);
  assert.ok(withAttr(html, "role", "status").length >= 5, "each card announces that it is loading");
  assert.match(text, /Loading GET \/system/);
  assert.match(text, /Loading runs/);
  assert.match(text, /Loading GET \/providers/);
  // The demo call to action is there from the start.
  assert.ok(hrefs(html).includes("#/live?panel=replay"));
});

test("readiness: the simulated shop is active, Kind and SREGym are planned for M5 and not operational", () => {
  const html = overview();
  const text = textOf(html);
  assert.match(text, /Simulated environment · this page only reads/);
  assert.match(text, /No real cluster, Kubernetes or production system is touched/);
  const simulated = between(html, "Simulated shop", "SREGym");
  assert.match(textOf(simulated), /configured Active Operational/);
  const kind = between(html, "Kind (local Kubernetes)</span>", "</li>");
  assert.match(textOf(kind), /Planned · M5 Not operational/);
  assert.doesNotMatch(textOf(kind), /Active/);
  const sregym = between(html, ">SREGym</span>", "</li>");
  assert.match(textOf(sregym), /Planned · M5 Not operational/);
  assert.match(text, /12 active · 1 available · 1 disabled/);
  assert.match(text, /OpenTelemetry console spans \(TraceSink\) Disabled/);
  assert.match(text, /Versions RECAP 0\.4\.0 prompts prompts-v4\.7 policy policy-v1 API recap-api-v1 code 673bb8ad878a/);
});

test("the demo starts from the golden recording, labelled as a replay of a real model on a simulated target", () => {
  const html = overview();
  const text = textOf(html);
  assert.equal(demoFixture(REPLAYS)?.id, "qwen3-8b-unfamiliar-parallel");
  assert.equal(demoHref("qwen3-8b-unfamiliar-parallel"), "#/live?panel=replay&fixture=qwen3-8b-unfamiliar-parallel");
  assert.equal(count(html, 'href="#/live?panel=replay&amp;fixture=qwen3-8b-unfamiliar-parallel"'), 2, "header button and card button");
  const card = between(html, "Start the demo</h2>", "System readiness");
  const cardText = textOf(card);
  assert.match(cardText, /REPLAY · RECORDED REAL-LLM RUN qwen3:8b \/ SIMULATED TARGET/);
  assert.match(cardText, /Unfamiliar incident, two specialists, owner-approved fix/);
  assert.match(cardText, /no model is called now/);
  assert.match(cardText, /Original fix approved by the owner, at an interactive terminal \(162\.5 s after the request\)\. The replay asks you again\./);
  assert.match(cardText, /Recording intact 2 of 2 committed recordings can be replayed here/);
  assert.ok(hrefs(card).includes("https://github.com/codeit-ronit/recap/tree/main/evals/live/2026-10-25-qwen3-8b-human-approved-unfamiliar-prompts-v4.7"));
  assert.ok(hrefs(card).includes("https://github.com/codeit-ronit/recap/blob/main/docs/demo-walkthrough.md"));
  assert.match(text, /Starting a replay needs the operator token/);
});

test("when no recording can be replayed the demo card says why, and still links the Incident Lab", () => {
  const blocked: dto.ReplaysView = {
    ...REPLAYS,
    fixtures: [fixture({ launchable: false, integrity: "altered", not_launchable_because: ["the file's SHA-256 does not match the manifest"] })],
  };
  const html = overview({ replays: polled(blocked) });
  const card = textOf(between(html, "Start the demo</h2>", "System readiness"));
  assert.match(card, /No recording can be replayed here/);
  assert.match(card, /the file's SHA-256 does not match the manifest/);
  assert.ok(hrefs(html).includes("#/live?panel=replay"));
  const none = overview({ replays: polled({ ...REPLAYS, fixtures: [] }) });
  assert.match(textOf(none), /No recording to replay fixtures\/recordings\/MANIFEST\.json lists no committed recording\./);
});

test("recent runs: the ten newest, each opening the Trace Explorer, with its data source and duration", () => {
  const html = overview();
  assert.equal(RECENT_RUNS, 10);
  assert.equal(count(html, 'class="ov-run"'), 10);
  assert.match(textOf(html), /The 10 newest of 11 runs\./);
  assert.ok(hrefs(html).includes("#/timeline?run=run-69960d199f"));
  assert.ok(!hrefs(html).includes("#/timeline?run=run-0ldest0001"), "the eleventh run is not listed");
  const first = textOf(between(html, 'href="#/timeline?run=run-69960d199f"', 'href="#/timeline?run=run-f9395a4471"'));
  assert.match(first, /bad-deployment , open run run-69960d199f in the Trace Explorer/);
  assert.match(first, /Started 28 min ago/);
  assert.match(first, /Outcome: Resolved/);
  assert.match(first, /REPLAY · RECORDED REAL-LLM RUN qwen3:8b \/ SIMULATED TARGET/);
  assert.match(first, /Duration: 842 ms/);
  const scripted = textOf(between(html, 'href="#/timeline?run=run-6539891f82"', 'href="#/timeline?run=run-debd9efab6"'));
  assert.match(scripted, /Safety blocked/);
  assert.match(scripted, /SCRIPTED BASELINE \/ SIMULATED TARGET/);
  // Every run in the list carries a simulated-target label: nothing suggests a real system.
  assert.equal(count(between(html, 'class="ov-runs-list"', "</ol>"), "SIMULATED TARGET"), 10);
});

test("outcomes are counted per data source: no blended success rate, no percentage", () => {
  const html = overview();
  const card = between(html, "Outcomes by data source</h2>", "Access</h2>");
  const text = textOf(card);
  assert.match(text, /Scripted baseline .*9 runs · 9 scenarios Resolved 7 Safety blocked 1 Budget exhausted 1/);
  assert.match(text, /Replay .*2 runs · 2 scenarios Resolved 2/);
  assert.match(text, /These 2 replays re-ran 2 recordings: a recording replayed again is not a new model result\./);
  assert.match(text, /Local LLM .*0 runs No runs from this source yet\./);
  assert.match(text, /Free hosted LLM .*0 runs No runs from this source yet\./);
  assert.match(text, /none is a success rate, and the sources are never added together/);
  assert.doesNotMatch(textOf(html), /%/, "no rate anywhere on the page");
  assert.doesNotMatch(text, /11 runs/, "the sources are never summed");
  // Sources without runs are drawn as empty (dashed), not hidden.
  assert.equal(count(card, 'class="ov-source is-empty"'), 2);
});

test("a run that recorded no source, target or wall time reads Not recorded", () => {
  const partial = summary({
    run_id: "run-m1old00001",
    scenario_id: "bad-deployment",
    provider_class: null,
    provider_kind: null,
    environment_kind: null,
    environment_simulated: null,
    wall_seconds: null,
    ended_at: null,
    not_recorded: ["provider_info", "environment", "ended_at"],
  });
  const html = overview({ runs: polled(page([partial])) });
  const row = textOf(between(html, 'href="#/timeline?run=run-m1old00001"', "</li>"));
  assert.match(row, /SOURCE NOT RECORDED \/ TARGET NOT RECORDED/);
  assert.match(row, /Duration: Not recorded/);
  const card = textOf(between(html, "Outcomes by data source</h2>", "Access</h2>"));
  assert.match(card, /Source not recorded .*1 run · 1 scenario Resolved 1/);
  assert.match(card, /whose decision source was not recorded/);
});

test("the breakdown says when it counted only the newest runs", () => {
  const html = overview({ runs: polled(page(RUNS, 1234)) });
  assert.match(textOf(html), /Counted over the newest 11 of 1,234 runs\./);
  assert.match(textOf(html), /The 10 newest of 1,234 runs\./);
});

test("active incidents: listed first, with their state, liveness and a link to the Incident Lab", () => {
  const live = summary({
    run_id: "run-live000001",
    scenario_id: "m3-inventory-leak-behind-frontend",
    state: "AWAITING_APPROVAL",
    status: "running",
    terminal: false,
    outcome: "in_progress",
    pending_approval: true,
    ended_at: null,
    wall_seconds: null,
    provider_class: "replay",
    provider_kind: "replay",
    model: "qwen3:8b",
  });
  const html = overview({ runs: polled(page([live, ...RUNS], 12)) });
  const text = textOf(html);
  assert.match(text, /1 incident in progress · 1 awaiting approval/);
  const card = between(html, 'id="ov-active"', "Start the demo</h2>");
  const cardText = textOf(card);
  assert.match(cardText, /Active incidents 1/);
  assert.match(cardText, /Awaiting approval Running in this server REPLAY · RECORDED REAL-LLM RUN qwen3:8b \/ SIMULATED TARGET/);
  assert.equal(count(cardText, "Awaiting approval"), 1, "the state says it once");
  assert.ok(hrefs(card).includes("#/live?run=run-live000001"));
  // The header's status is the one polite live region (relative times in the card do not re-announce).
  assert.equal(withAttr(card, "aria-live").length, 0);
  assert.match(between(html, 'class="ov-pill-status" role="status"', "</span></a></span>"), /1 incident in progress · 1 awaiting approval/);
  // The live run's duration is "running", its outcome "In progress" with its state.
  const row = textOf(between(html, 'href="#/timeline?run=run-live000001"', "</li>"));
  assert.match(row, /Outcome: In progress Awaiting approval/);
  assert.match(row, /Duration: running/);
  // With nothing running, the header says so and links the Incident Lab.
  const idle = overview();
  assert.match(textOf(idle), /No incident running/);
  assert.ok(hrefs(idle).includes("#/live"));
  assert.doesNotMatch(idle, /id="ov-active"/);
});

test("access: reads are open, acting needs the operator token, and the tab's state is shown", () => {
  const html = overview();
  const card = textOf(between(html, "Access</h2>", "Not built yet: M5</h2>"));
  assert.match(card, /Reading every page Open/);
  assert.match(card, /Acting: start a run or a replay, approve or reject a fix, cancel a run, start an evaluation Operator token required/);
  assert.match(card, /This browser tab Signed out/);
  const signedIn: SessionState = { kind: "signed_in", operator: "owner" };
  const inHtml = overview({ session: signedIn });
  assert.match(textOf(inHtml), /This browser tab Signed in as owner/);
  assert.doesNotMatch(inHtml, /Bearer/);
  // The session state is derived without ever exposing the token.
  assert.deepEqual(sessionState(null, polled(undefined)), { kind: "signed_out" });
  assert.deepEqual(sessionState("tok-secret", polled(undefined)), { kind: "checking" });
  assert.deepEqual(sessionState("tok-secret", polled({ operator: "owner" })), { kind: "signed_in", operator: "owner" });
  assert.deepEqual(sessionState("tok-secret", polled(undefined, new ApiError("Sign in", 401, "/auth/session", null, false))), { kind: "rejected" });
  assert.doesNotMatch(JSON.stringify(sessionState("tok-secret", polled({ operator: "owner" }))), /tok-secret/);
});

test("M5 gaps list what the server reports as planned and link the plan", () => {
  const html = overview();
  const card = between(html, "Not built yet: M5</h2>", "Decision providers</h2>");
  const text = textOf(card);
  assert.match(text, /SREGym adapter Planned · M5/);
  assert.match(text, /Kind \(local Kubernetes\) adapter Planned · M5/);
  assert.doesNotMatch(text, /Evaluation mode/, "every evaluation mode is implemented since M5");
  assert.match(text, /documentation only: nothing in it is built, installed or provisioned/);
  const plan = tags(card, "a").find((a) => a.attrs.href === "https://github.com/codeit-ronit/recap/blob/main/docs/m5-integration-plan.md");
  assert.ok(plan, "links docs/m5-integration-plan.md");
  assert.equal(plan?.attrs.target, "_blank");
  assert.equal(plan?.attrs.rel, "noopener noreferrer");
});

test("providers: class, model, readiness and per-run selection; keys only as set or missing", () => {
  const html = overview();
  const card = between(html, "Decision providers</h2>", "</section>");
  assert.equal(count(card, 'class="ov-provider"'), 5);
  const names = [...card.matchAll(/class="ov-provider-name"><code class="mono">([^<]+)<\/code>/g)].map((m) => m[1]);
  assert.deepEqual(names, ["deterministic", "replay", "ollama", "groq", "anthropic"], "the default first, then by class");
  const text = textOf(card);
  assert.match(text, /deterministic Server default/);
  assert.match(text, /SCRIPTED BASELINE/);
  assert.match(text, /REPLAY · RECORDED REAL-LLM RUN recorded model qwen3:8b/);
  assert.match(text, /LOCAL LLM model qwen3:8b · tools mode/);
  assert.match(text, /FREE HOSTED LLM model openai\/gpt-oss-120b · json mode/);
  assert.match(text, /PAID LLM model claude-opus-5-5 · json mode/);
  assert.match(text, /Needs setup set GROQ_API_KEY in the environment API key missing owner opt-in: no · free-plan confirmation: missing/);
  assert.match(text, /Disabled paid API: off for this \$0 project API key not checked/);
  assert.equal(count(text, "Selectable per run"), 3);
  assert.equal(count(text, "Not selectable"), 2);
  assert.match(text, /Last 24 h \(RECAP's usage ledger\): 12 requests · 48,210 tokens reported/);
  assert.equal(count(card, "<details"), 5, "each provider's limitations behind a disclosure");
  // Never a key value, never the model server's address.
  assert.doesNotMatch(card, /127\.0\.0\.1:11434/);
});

test("empty and error states say why, and failed requests offer a retry", () => {
  const empty = overview({ runs: polled(page([], 0)) });
  const text = textOf(empty);
  assert.match(text, /No runs yet This server's data folder holds no run yet\./);
  assert.match(text, /Nothing to count yet No run has been recorded, so there is no outcome to count\./);
  const failing = new ApiError("Internal Server Error", 500, "/providers", null, false);
  const broken = overview({ providers: polled(undefined, failing), system: polled(undefined, new ApiError("boom", 500, "/system", null, false)) });
  const brokenText = textOf(broken);
  assert.match(brokenText, /Could not load GET \/providers · HTTP 500 Internal Server Error/);
  assert.match(brokenText, /Could not load GET \/system · HTTP 500 boom/);
  assert.ok(withAttr(broken, "role", "alert").length >= 4, "readiness, access, M5 gaps and providers each report the failure");
  assert.ok(count(brokenText, "Retry") >= 4);
  // No notice is invented while /system is unknown.
  assert.doesNotMatch(brokenText, /Simulated environment · this page only reads/);
  const noProviders = overview({ providers: polled([]) });
  assert.match(textOf(noProviders), /No provider configured configs\/providers\.yaml lists no decision provider\./);
});

test("a live environment adapter is never presented as simulated", () => {
  const live = { ...SYSTEM, environment: { ...SYSTEM.environment, is_simulated: false, kind: "kind" }, environment_kind: "kind", simulation_notice: null };
  const html = overview({ system: polled(live) });
  const text = textOf(html);
  assert.match(text, /Live environment adapter: kind/);
  assert.doesNotMatch(text, /this page only reads/);
});

test("hostile text from the server is escaped, never markup or a link", () => {
  const evil = "<script>alert(1)</script>";
  const html = overview({
    runs: polled(page([summary({ run_id: "run-evil000001", scenario_id: evil, label: '<img src=x onerror="alert(2)">' })])),
    providers: polled([provider({ name: "evil", kind: "llm", origin: "local_llm", description: '<b onmouseover="alert(3)">hi</b>', detail: "javascript:alert(4)", limitations: [evil] })]),
    replays: polled({ ...REPLAYS, fixtures: [fixture({ title: evil, description: '<a href="javascript:alert(5)">x</a>' })] }),
    system: polled({ ...SYSTEM, components: [...COMPONENTS.slice(0, 4), component("hybrid_retrieval", "Embedder", evil, "unavailable", '<iframe src="x">')] }),
  });
  assert.equal(tags(html, "script").length, 0);
  assert.equal(tags(html, "iframe").length, 0);
  assert.equal(tags(html, "img").length, 0);
  assert.equal(withAttr(html, "onerror").length, 0);
  assert.equal(withAttr(html, "onmouseover").length, 0);
  assert.ok(hrefs(html).every((href) => !/^\s*javascript:/i.test(href)));
  const text = textOf(html);
  assert.ok(text.includes(evil), "the text is shown, escaped");
  assert.ok(text.includes('<b onmouseover="alert(3)">hi</b>'));
});

// --- Architecture & Learning ------------------------------------------------------------------

function architecture(selected: Parameters<typeof ArchitectureView>[0]["selected"], system = polled(SYSTEM)): string {
  return render(<ArchitectureView system={system} selected={selected} onSelect={() => undefined} />);
}

const tabsOf = (html: string) => tags(html, "button").filter((b) => b.attrs.role === "tab");

test("the flow is a vertical tab list: eleven steps plus two around them, one selected", () => {
  const html = architecture("alert");
  assert.equal(withAttr(html, "role", "tablist").length, 1);
  assert.equal(withAttr(html, "aria-orientation", "vertical").length, 1);
  const tabs = tabsOf(html);
  assert.equal(tabs.length, 13);
  assert.deepEqual(
    tabs.map((t) => t.attrs.id),
    [
      "amap-tab-alert",
      "amap-tab-controller",
      "amap-tab-retrieval",
      "amap-tab-routing",
      "amap-tab-specialists",
      "amap-tab-gateway",
      "amap-tab-verifier",
      "amap-tab-approval",
      "amap-tab-write",
      "amap-tab-oracle",
      "amap-tab-episode",
      "amap-tab-environment",
      "amap-tab-telemetry",
    ],
  );
  const selected = tabs.filter((t) => t.attrs["aria-selected"] === "true");
  assert.equal(selected.length, 1);
  assert.equal(selected[0].attrs.tabindex, "0");
  assert.ok(tabs.filter((t) => t !== selected[0]).every((t) => t.attrs.tabindex === "-1"), "roving tab index");
  assert.ok(tabs.every((t) => t.attrs["aria-controls"] === "amap-panel" && t.attrs.type === "button"));
  const panel = withAttr(html, "role", "tabpanel");
  assert.equal(panel.length, 1);
  assert.equal(panel[0].attrs["aria-labelledby"], "amap-tab-alert-title");
  assert.match(textOf(html), /Step 1: Alert/);
});

test("each step shows the status /system reports; the controller has none of its own", () => {
  const html = architecture("alert");
  const text = textOf(html);
  assert.match(text, /Step 1: Alert untrusted data Active/);
  assert.match(text, /Step 2: Incident controller trusted code No status in \/system/);
  assert.match(text, /Step 3: Retrieval & memory untrusted data Active BM25 keyword search/);
  assert.match(text, /Step 5: Investigation agents read-only agents Active/);
  assert.match(text, /Step 8: Human approval human decision Active/);
  assert.match(text, /Runbook search Active Hybrid search Available Experience memory Active/);
  assert.match(text, /Step 6: MCP tool gateway trusted code Active MCP over stdio/);
  assert.match(text, /Step 9: Authorized write trusted code Active Ed25519-signed, single-use capabilities/);
  assert.match(text, /Trace sinks Active OpenTelemetry Disabled/);
  // The target environment's planned adapters are drawn as planned, never active.
  const env = between(html, 'id="amap-tab-environment"', "</button>");
  assert.match(textOf(env), /Simulated shop Active SREGym Planned · M5 Kind \(local Kubernetes\) Planned · M5/);
  assert.equal(count(env, 'class="amap-part is-planned"'), 2);
});

test("the target environment's panel: adapters with their status, operational or not, and the M5 plan", () => {
  const html = architecture("environment");
  const panel = between(html, 'id="amap-panel"', "Live wiring</h2>");
  const text = textOf(panel);
  assert.match(text, /Around every step untrusted data Target environment/);
  assert.match(text, /Simulated shop adapter configured Active Operational class SimulatedEnvironment/);
  assert.match(text, /SREGym adapter Planned · M5 Not operational class SREGymEnvironment interface only/);
  assert.match(text, /Kind \(local Kubernetes\) adapter Planned · M5 Not operational no adapter in this repository/);
  assert.match(text, /nothing in RECAP runs on a real cluster/);
  assert.ok(hrefs(panel).includes("https://github.com/codeit-ronit/recap/blob/main/docs/m5-integration-plan.md"));
  assert.ok(hrefs(panel).includes("https://github.com/codeit-ronit/recap/tree/main/integrations/sregym"));
});

test("a step's panel explains it: what it does, server facts, code, ADRs, trace events, where to see it", () => {
  const html = architecture("verifier");
  const panel = between(html, 'id="amap-panel"', "Live wiring</h2>");
  const text = textOf(panel);
  assert.match(text, /Step 7 of 11 trusted code Evidence verifier/);
  assert.match(text, /Status in GET \/system Verifier Active evidence-rule-verifier-v1/);
  assert.match(text, /never by votes/);
  assert.match(text, /An open disagreement sends one question back to the router \(step 4\)\./);
  assert.match(text, /This server \(GET \/system\) Verifier evidence-rule-verifier-v1/);
  assert.match(text, /In a run's trace verification\.merge verification\.evidence/);
  const links = hrefs(panel);
  assert.ok(links.includes("https://github.com/codeit-ronit/recap/blob/main/recap/agents/verifier.py"));
  assert.ok(links.includes("https://github.com/codeit-ronit/recap/blob/main/docs/decisions/0021-evidence-rules-settle-disagreements-never-votes.md"));
  assert.ok(links.includes("#/evidence"), "see it in Evidence & Decisions");
  assert.match(text, /See it in Evidence & Decisions/);
  // Every link to the repository opens in a new tab without an opener.
  for (const a of tags(html, "a").filter((t) => t.attrs.href?.startsWith("https://"))) {
    assert.equal(a.attrs.target, "_blank");
    assert.equal(a.attrs.rel, "noopener noreferrer");
  }
});

test("the specialists' panel names the default provider from /system and who may be picked", () => {
  const text = textOf(between(architecture("specialists"), 'id="amap-panel"', "Live wiring</h2>"));
  assert.match(text, /LLMProvider Active deterministic-scripted-policy the scripted baseline: not an LLM/);
  assert.match(text, /New runs use, unless they pick another provider: SCRIPTED BASELINE deterministic-scripted-policy/);
  assert.match(text, /Providers a run may pick deterministic, ollama, replay/);
  assert.match(text, /Prompt version \(real models\) prompts-v4\.7/);
});

test("the episode panel shows which partitions each kind of run reads and writes", () => {
  const text = textOf(between(architecture("episode"), 'id="amap-panel"', "Live wiring</h2>"));
  assert.match(text, /Scripted baseline reads train, demo · writes demo/);
  assert.match(text, /Local LLM reads train, live · writes live/);
  assert.match(text, /Replay reads train, live · writes nothing/);
});

test("the page links the walkthrough and the ADR index, and lists the live wiring with versions", () => {
  const html = architecture("alert");
  const links = hrefs(html);
  assert.ok(links.includes("https://github.com/codeit-ronit/recap/blob/main/docs/flow.md"));
  assert.ok(links.includes("https://github.com/codeit-ronit/recap/blob/main/docs/decisions/README.md"));
  const wiring = between(html, "Live wiring</h2>", "Learn more</h2>");
  assert.equal(count(wiring, 'class="amap-wire"'), 14);
  assert.match(textOf(wiring), /Embedder ollama:nomic-embed-text \(local, loopback\) \+ reciprocal rank fusion stored vectors cover every runbook chunk Available/);
  assert.match(textOf(wiring), /Code version 673bb8ad878a/);
  // A long interface name may wrap only after a "." or "_".
  assert.match(wiring, /TargetEnvironment\.<wbr\/>execute_<wbr\/>authorized/);
  const learn = textOf(between(html, "Learn more</h2>", "</section>"));
  assert.match(learn, /End-to-end walkthrough/);
  assert.match(learn, /M5 integration plan .*Documentation only\./);
});

test("a planned component is drawn dashed and never active", () => {
  const planned = { ...SYSTEM, components: COMPONENTS.map((c) => (c.id === "write_boundary" ? { ...c, status: "planned" as const } : c)) };
  const html = architecture("write", polled(planned));
  const tab = tabsOf(html).find((t) => t.attrs.id === "amap-tab-write");
  assert.match(tab?.attrs.class ?? "", /is-planned/);
  const tabText = textOf(between(html, 'id="amap-tab-write"', "</button>"));
  assert.match(tabText, /Authorized write trusted code Planned/);
  assert.doesNotMatch(tabText, /Active/);
  assert.match(between(html, "Live wiring</h2>", "Learn more</h2>"), /class="amap-wire is-planned"/);
});

test("without /system the flow still teaches, and its statuses say why they are missing", () => {
  const loading = architecture("alert", polled(undefined));
  assert.equal(tabsOf(loading).length, 13);
  assert.match(textOf(loading), /Step 1: Alert untrusted data Checking…/);
  assert.match(textOf(loading), /Waiting for GET \/system…/);
  const failed = architecture("alert", polled(undefined, new ApiError("Internal Server Error", 500, "/system", null, false)));
  const text = textOf(failed);
  assert.match(text, /Could not load GET \/system: statuses are not shown · HTTP 500/);
  assert.match(text, /Retry/);
  assert.match(text, /Step 1: Alert untrusted data Status unavailable/);
  assert.doesNotMatch(between(failed, 'role="tablist"', 'id="amap-panel"'), /Active/);
  // The learning content stays: what it does, code and ADRs.
  assert.match(text, /The alert's words are data, never instructions/);
  assert.ok(hrefs(failed).includes("https://github.com/codeit-ronit/recap/blob/main/docs/decisions/0007-replaceable-environment-adapter.md"));
});

test("server text in the wiring is escaped", () => {
  const evil = { ...SYSTEM, components: [component("router", "RouterPolicy", "<script>alert(1)</script>", "active", '<img src=x onerror="alert(2)">')] };
  const html = architecture("routing", polled(evil));
  assert.equal(tags(html, "script").length, 0);
  assert.equal(withAttr(html, "onerror").length, 0);
  assert.ok(textOf(html).includes("<script>alert(1)</script>"));
});

test("the page selects the step a deep link names", () => {
  const html = render(<ArchitecturePage route={parseHash("#/architecture?node=oracle")} system={polled(undefined)} />);
  const selected = tabsOf(html).filter((t) => t.attrs["aria-selected"] === "true");
  assert.deepEqual(
    selected.map((t) => t.attrs.id),
    ["amap-tab-oracle"],
  );
  const fallback = render(<ArchitecturePage route={parseHash("#/architecture?node=nope")} system={polled(undefined)} />);
  assert.deepEqual(
    tabsOf(fallback)
      .filter((t) => t.attrs["aria-selected"] === "true")
      .map((t) => t.attrs.id),
    ["amap-tab-alert"],
  );
});

test("Breakable adds break points only after dots and underscores", () => {
  assert.equal(render(<Breakable text="infrastructure_investigator" />), "infrastructure_<wbr/>investigator");
  assert.equal(render(<Breakable text="plain" />), "plain");
});
