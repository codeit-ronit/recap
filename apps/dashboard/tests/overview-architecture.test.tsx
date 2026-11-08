/**
 * The Overview and Architecture & Learning pages, rendered with server answers shaped exactly
 * like src/dto.ts (GET /system, /providers, /runs, /replays, /scenarios and POST
 * /environment/check): the seeded simulated server (the Kind and SREGym adapters implemented,
 * not configured), and the same server with RECAP_TARGET=kind-demo or sregym-hotel. Covered:
 * readiness, the active environment and its Check now button in every state, the demo or the
 * real-cluster call to action, providers without keys, the newest runs with their data source
 * and target, outcomes counted per source and target (never one success rate), active
 * incidents, access, and the flow map (a vertical tab list whose statuses come from /system)
 * with each target's explanation and how a real Kubernetes action is authorized. Also:
 * loading / empty / error states, partial records ("Not recorded") and hostile text.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import type { ReactElement, ReactNode } from "react";
import { ApiError } from "../src/api";
import type { Page } from "../src/api";
import { SIGN_IN_HINT } from "../src/auth";
import type * as dto from "../src/dto";
import { parseHash } from "../src/hooks";
import { ArchitecturePage, ArchitectureView, Breakable } from "../src/pages/Architecture";
import {
  EnvironmentCheckPanel,
  NO_CHECK,
  OverviewPage,
  OverviewView,
  RECENT_RUNS,
  demoFixture,
  demoHref,
  sessionState,
} from "../src/pages/Overview";
import type { EnvironmentCheckState, OverviewViewProps, ScenarioRef } from "../src/pages/Overview";
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

const SIMULATED_DETAIL = "deterministic local simulation of a five-service shop (recap/environments/simulated/)";
const SREGYM_DETAIL =
  "a SREGym 1.1 problem on SREGym's own Kind cluster, deployed and injected by its external harness: RECAP's Kubernetes adapter with image-tag releases and no probe or metrics, three RBAC-limited identities of its own, and SREGym's mitigation oracle as the benchmark's grader (integrations/sregym/, ADR 0031); chosen with RECAP_TARGET=sregym-hotel. GET /system never contacts the cluster";
const KIND_DETAIL =
  "a local Kind cluster through typed kubectl operations, four RBAC-limited identities and an HTTP probe of its own (recap/environments/kubernetes/); chosen with RECAP_TARGET=kind-demo. GET /system never contacts the cluster: whether it is up is checked when a run starts";

/** GET /system `environment_adapters` as recap/runtime.py reports them with `configured` active. */
function adaptersFor(configured: "simulated" | "kind" | "sregym"): dto.AdapterStatus[] {
  const entry = (kind: string, adapter: string, detail: string): dto.AdapterStatus => ({
    kind,
    adapter,
    configured: kind === configured,
    status: kind === configured ? "active" : "available",
    operational: kind === configured,
    milestone: null,
    detail,
  });
  return [
    entry("simulated", "SimulatedEnvironment", SIMULATED_DETAIL),
    entry("sregym", "SREGymEnvironment", SREGYM_DETAIL),
    entry("kind", "KubernetesEnvironment", KIND_DETAIL),
  ];
}

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
  environment_adapters: adaptersFor("simulated"),
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

/** The components of a server on a real cluster: its environment, oracle and policy. */
function clusterComponents(name: string, kind: string, scenarios: string, policy: string): dto.ComponentStatus[] {
  return COMPONENTS.map((c) => {
    if (c.id === "environment") return { ...c, implementation: `${name} (kind ${kind})`, detail: `${scenarios}; NOT simulated` };
    if (c.id === "oracle") return { ...c, implementation: `${kind}-health-oracle` };
    if (c.id === "safety_policy") return { ...c, implementation: `recap-policy-engine (${policy})` };
    return c;
  });
}

/** The server with RECAP_TARGET=kind-demo (Runtime.system_summary, offline: GET /system never contacts the cluster). */
const KIND_SYSTEM: dto.SystemView = {
  ...SYSTEM,
  environment: {
    kind: "kind",
    name: "Kind cluster recap-m5",
    namespace: "recap-demo",
    is_simulated: false,
    supported_actions: ["rollback", "restart"],
    description:
      "Local Kind cluster recap-m5 (context kind-recap-m5), not production: 2 demo services in namespace recap-demo, read through typed kubectl operations. The only writes are a rollback to a named revision and a rolling restart, each with a signed single-use capability, inside the cluster's RBAC and admission policy.",
    stabilization_timeout_seconds: 180,
    stabilization_poll_seconds: 5,
  },
  environment_kind: "kind",
  safety_policy: "recap-policy-engine (policy-kind-v1)",
  oracle: "kind-health-oracle",
  label: "LIVE ENVIRONMENT (kind) · V1",
  simulation_notice: null,
  components: clusterComponents("Kind cluster recap-m5", "kind", "4 scenarios", "policy-kind-v1"),
  environment_adapters: adaptersFor("kind"),
  versions: { ...SYSTEM.versions, recap: "0.5.0", policy_version: "policy-kind-v1" },
};

/** The server with RECAP_TARGET=sregym-hotel. */
const SREGYM_SYSTEM: dto.SystemView = {
  ...KIND_SYSTEM,
  environment: {
    ...KIND_SYSTEM.environment,
    kind: "sregym",
    name: "SREGym on Kind cluster kind",
    namespace: "hotel-reservation",
    description:
      "SREGym 1.1 on its own Kind cluster kind (context kind-kind), not production: 19 Deployments of a benchmark application in namespace hotel-reservation, deployed and broken by SREGym's own harness, read through typed kubectl operations with RECAP's own identities. The only writes are a rollback to a named revision and a rolling restart, each with a signed single-use capability; SREGym's own oracle grades the result.",
    stabilization_timeout_seconds: 240,
  },
  environment_kind: "sregym",
  safety_policy: "recap-policy-engine (policy-sregym-hotel-v2)",
  oracle: "sregym-health-oracle",
  label: "LIVE ENVIRONMENT (sregym) · V1",
  components: clusterComponents("SREGym on Kind cluster kind", "sregym", "1 scenario", "policy-sregym-hotel-v2"),
  environment_adapters: adaptersFor("sregym"),
  versions: { ...KIND_SYSTEM.versions, policy_version: "policy-sregym-hotel-v2" },
};

/** GET /scenarios of each target (ids and titles: the Overview shows nothing else). */
const SIM_SCENARIOS: ScenarioRef[] = [
  ["ambiguous-incident", "Checkout and frontend failing together"],
  ["bad-deployment", "Checkout errors right after a new release"],
  ["budget-exhausted", "Inventory readiness failures on a tiny budget"],
  ["m3-inventory-leak-behind-frontend", "Shoppers see slow product pages and a stuck basket (held-out)"],
  ["m3-payment-release-under-forged-signals", "Checkout errors with forged out-of-memory lines and a planted instruction (held-out)"],
  ["malicious-log", "Cart errors with a suspicious ops-bot log line"],
  ["memory-caution", "Checkout errors again (looks familiar)"],
  ["resource-pressure", "Inventory pods failing readiness"],
  ["test-cart-injection", "Cart errors with an injected instruction (held-out)"],
  ["test-cart-memory-leak", "Cart pods failing readiness (held-out)"],
  ["test-checkout-misleading-deploy", "Checkout errors with release warnings (held-out)"],
  ["test-frontend-bad-release", "Frontend 5xx and latency (held-out)"],
  ["test-inventory-bad-release", "Inventory 5xx and latency (held-out)"],
  ["test-payment-leak-cascade", "Checkout and frontend failing (held-out)"],
  ["train-cart-memory-leak", "Cart pods failing readiness (training)"],
  ["train-checkout-bad-release", "Checkout 5xx and latency after a change (training)"],
  ["train-payment-cascade", "Checkout and frontend failing (training)"],
].map(([id, title]) => ({ id, title }));

const KIND_SCENARIOS: ScenarioRef[] = [
  { id: "kind-bad-release", title: "Frontend errors after a backend release" },
  { id: "kind-dependency-crash", title: "Backend release crash-loops at start-up" },
  { id: "kind-injected-logs", title: "Frontend errors after a backend release, with an instruction in its logs" },
  { id: "kind-report-leak", title: "Backend stops serving after a burst of reports" },
];

const SREGYM_SCENARIOS: ScenarioRef[] = [{ id: "sregym-misconfig-app-hotel-res", title: "SREGym 1.1 problem misconfig_app_hotel_res (hotel reservation)" }];

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

/** A run of the self-hosted qwen3:8b on the owner's VM (provider ollama-vm) against a real cluster. */
function clusterRun(kind: "kind" | "sregym", over: Partial<dto.RunSummary> = {}): dto.RunSummary {
  return summary({
    scenario_id: kind === "kind" ? "kind-bad-release" : "sregym-misconfig-app-hotel-res",
    provider: "ollama-vm:qwen3:8b",
    provider_class: "local_llm",
    provider_kind: "llm",
    model: "qwen3:8b",
    prompt_version: "prompts-v4.7",
    environment_kind: kind,
    environment_simulated: false,
    outcome_note: "oracle verified recovery",
    model_requests: 8,
    wall_seconds: 905.2,
    labels: [`LIVE ENVIRONMENT (${kind})`, "LIVE LOCAL MODEL"],
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

/** The Kind server's folder: the W4 runs (a verified recovery, a safe failure, two rejections). */
const KIND_RUNS: dto.RunSummary[] = [
  clusterRun("kind", { run_id: "run-047aac6eb0" }),
  clusterRun("kind", { run_id: "run-9fea287988", state: "FAILED", outcome: "failed", recovered: false }),
  clusterRun("kind", { run_id: "run-e4bd2821df", state: "BLOCKED", outcome: "approval_rejected", recovered: false }),
  clusterRun("kind", { run_id: "run-16e2fb14b3", state: "BLOCKED", outcome: "approval_rejected", recovered: false }),
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

/** POST /environment/check answers, 12 s before NOW. */
const PASSED: dto.EnvironmentCheck = {
  checked_at: "2026-10-26T05:29:48.000000Z",
  environment_kind: "kind",
  simulated: false,
  ok: true,
  checks: [{ name: "preflight", ok: true, detail: "credentials pinned to this cluster; API server and versions answer as expected" }],
};
const FAILED: dto.EnvironmentCheck = { ...PASSED, ok: false, checks: [{ name: "preflight", ok: false, detail: "no answer within 20 s (tunnel down?)" }] };
const SIGNED_IN: SessionState = { kind: "signed_in", operator: "owner" };

function page(items: dto.RunSummary[], total: number | null = items.length): Page<dto.RunSummary> {
  return { items, total };
}

function overview(over: Partial<OverviewViewProps> = {}): string {
  const props: OverviewViewProps = {
    system: polled(SYSTEM),
    providers: polled(PROVIDERS),
    runs: polled(page(RUNS)),
    replays: polled(REPLAYS),
    scenarios: polled(SIM_SCENARIOS),
    session: { kind: "signed_out" },
    check: NO_CHECK,
    onCheck: () => undefined,
    now: NOW,
    ...over,
  };
  return render(<OverviewView {...props} />);
}

/** The Overview of the server with RECAP_TARGET=kind-demo (or another real-cluster system). */
function clusterOverview(system: dto.SystemView, over: Partial<OverviewViewProps> = {}): string {
  return overview({
    system: polled(system),
    runs: polled(page(system.environment_kind === "kind" ? KIND_RUNS : [])),
    scenarios: polled(system.environment_kind === "kind" ? KIND_SCENARIOS : SREGYM_SCENARIOS),
    ...over,
  });
}

/** The markup of the first element whose opening tag carries `marker`, up to the next sibling marker. */
function between(html: string, start: string, end: string): string {
  const from = html.indexOf(start);
  assert.ok(from >= 0, `missing ${start}`);
  const to = html.indexOf(end, from + start.length);
  return html.slice(from, to === -1 ? undefined : to);
}

const hrefs = (html: string) => tags(html, "a").map((a) => a.attrs.href ?? "");
/** The System readiness card (under Technical details). */
const readinessOf = (html: string) => between(html, "System readiness</h2>", "Outcomes by data source</h2>");
const github = (path: string) => `https://github.com/codeit-ronit/recap/${path.endsWith("/") ? "tree" : "blob"}/main/${path.replace(/\/$/, "")}`;

// --- Overview ---------------------------------------------------------------------------------

test("before the first answer every card shows its loading state", () => {
  const html = render(<OverviewPage route={parseHash("#/overview")} system={polled(undefined)} />);
  const text = textOf(html);
  assert.match(text, /Overview/);
  assert.ok(withAttr(html, "role", "status").length >= 5, "each card announces that it is loading");
  assert.match(text, /Loading GET \/system/);
  assert.match(text, /Loading runs/);
  assert.match(text, /Loading GET \/providers/);
  // Until GET /system answers, the demo call to action stands (its recordings say whether
  // they can be replayed here); nothing is called simulated or live meanwhile.
  assert.ok(hrefs(html).includes("#/live?panel=replay"));
  assert.doesNotMatch(text, /Run a real-cluster incident|Simulated environment · this page only reads|Live environment:/);
});

test("readiness: the simulated shop is active and operational; Kind and SREGym are implemented, available, not operational", () => {
  const html = overview();
  const text = textOf(html);
  assert.match(text, /Simulated environment · this page only reads/);
  assert.match(text, /No real cluster, Kubernetes or production system is touched/);
  const simulated = between(html, "Simulated shop", "SREGym");
  assert.match(textOf(simulated), /configured Active Operational/);
  const kind = between(html, "Kind (local Kubernetes)</span>", "</li>");
  assert.match(textOf(kind), /Available Not operational a local Kind cluster through typed kubectl operations/);
  assert.doesNotMatch(textOf(kind), /Active|Planned/);
  const sregym = between(html, ">SREGym</span>", "</li>");
  assert.match(textOf(sregym), /Available Not operational a SREGym 1\.1 problem on SREGym's own Kind cluster/);
  assert.doesNotMatch(textOf(sregym), /Planned|interface only|EnvironmentNotIntegrated/);
  assert.match(text, /12 active · 1 available · 1 disabled/);
  assert.match(text, /OpenTelemetry console spans \(TraceSink\) Disabled/);
  assert.match(text, /Versions RECAP 0\.4\.0 prompts prompts-v4\.7 policy policy-v1 API recap-api-v1 code 673bb8ad878a/);
});

test("the demo starts from the golden recording, labelled as a replay of a real model on a simulated target", () => {
  const html = overview();
  const text = textOf(html);
  assert.equal(demoFixture(REPLAYS)?.id, "qwen3-8b-unfamiliar-parallel");
  assert.equal(demoHref("qwen3-8b-unfamiliar-parallel"), "#/live?panel=replay&fixture=qwen3-8b-unfamiliar-parallel");
  assert.equal(count(html, 'href="#/live?panel=replay&amp;fixture=qwen3-8b-unfamiliar-parallel"'), 1, "the recording card's button, under Technical details");
  // The way in is the guided demo, from the hero: plain, interview mode, and the technology.
  const hero = between(html, '<section class="ov-hero"', "</section>");
  assert.match(textOf(hero), /RECAP is an AI reliability engineer that investigates incidents, proposes safe fixes and verifies recovery\./);
  assert.match(textOf(hero), /Try the guided demo Interview mode Real Kubernetes runs Explore the technology/);
  assert.deepEqual(hrefs(hero), ["#/demo", "#/demo?mode=interview", "#/published", "#/architecture"]);
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
  assert.doesNotMatch(text, /Run a real-cluster incident/, "a simulated server offers no real-cluster run");
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

test("recent runs: the five newest, each opening the Trace Explorer, with its data source and duration", () => {
  const html = overview();
  assert.equal(RECENT_RUNS, 5);
  assert.equal(count(html, 'class="ov-run"'), 5);
  assert.match(textOf(html), /The 5 newest of 11 runs\./);
  assert.ok(hrefs(html).includes("#/timeline?run=run-69960d199f"));
  assert.ok(!hrefs(html).includes("#/timeline?run=run-0ldest0001"), "the eleventh run is not listed");
  const first = textOf(between(html, 'href="#/timeline?run=run-69960d199f"', 'href="#/timeline?run=run-f9395a4471"'));
  assert.match(first, /bad-deployment , open run run-69960d199f in the Trace Explorer/);
  assert.match(first, /Started 28 min ago/);
  assert.match(first, /Outcome: Resolved/);
  assert.match(first, /REPLAY · RECORDED REAL-LLM RUN qwen3:8b \/ SIMULATED TARGET/);
  assert.match(first, /Duration: 842 ms/);
  const scripted = textOf(between(html, 'href="#/timeline?run=run-a4f9926bef"', "</li>"));
  assert.match(scripted, /Budget exhausted/);
  assert.match(scripted, /SCRIPTED BASELINE \/ SIMULATED TARGET/);
  assert.ok(!hrefs(html).includes("#/timeline?run=run-6539891f82"), "the sixth run is not listed");
  // Every run in the list carries a simulated-target label: nothing suggests a real system.
  assert.equal(count(between(html, 'class="ov-runs-list"', "</ol>"), "SIMULATED TARGET"), 5);
});

test("outcomes are counted per data source: no blended success rate, no percentage", () => {
  const html = overview();
  const card = between(html, "Outcomes by data source</h2>", "Active environment</h2>");
  const text = textOf(card);
  assert.match(text, /Scripted baseline · Simulated target SCRIPTED BASELINE \/ SIMULATED TARGET 9 runs · 9 scenarios Resolved 7 Safety blocked 1 Budget exhausted 1/);
  assert.match(text, /Replay · Simulated target .*2 runs · 2 scenarios Resolved 2/);
  assert.match(text, /These 2 replays re-ran 2 recordings: a recording replayed again is not a new model result\./);
  assert.match(text, /Local LLM LOCAL LLM 0 runs No runs from this source yet\./);
  assert.match(text, /Free hosted LLM FREE HOSTED LLM 0 runs No runs from this source yet\./);
  assert.match(text, /New inference by a self-hosted open-weight model \(Ollama\), on this machine or on the owner's own VM\./);
  assert.match(text, /none is a success rate, and no two sources or targets are ever added together/);
  assert.doesNotMatch(textOf(html), /%/, "no rate anywhere on the page");
  assert.doesNotMatch(text, /11 runs/, "the sources are never summed");
  // Sources without runs are drawn as empty (dashed), not hidden, and claim no target.
  assert.equal(count(card, 'class="ov-source is-empty"'), 2);
  assert.doesNotMatch(textOf(between(card, "Local LLM", "No runs from this source yet")), /TARGET/);
});

test("outcomes on a real cluster get their own column, apart from the same source on the simulator", () => {
  const runs = [clusterRun("kind", { run_id: "run-k1" }), clusterRun("kind", { run_id: "run-k2", outcome: "approval_rejected", state: "BLOCKED" }), summary({ provider_class: "local_llm", provider_kind: "llm", model: "qwen3:8b" }), clusterRun("sregym", { run_id: "run-s1" })];
  const html = overview({ runs: polled(page(runs)) });
  const card = between(html, "Outcomes by data source</h2>", "Active environment</h2>");
  const text = textOf(card);
  assert.match(text, /Local LLM · Simulated target LOCAL LLM \/ SIMULATED TARGET 1 run · 1 scenario Resolved 1/);
  assert.match(text, /Local LLM · Live target · Kind \(local Kubernetes\) LOCAL LLM \/ LIVE TARGET · kind 2 runs · 1 scenario Resolved 1 Approval rejected 1/);
  assert.match(text, /These runs acted on a real cluster, not a simulation\./);
  assert.match(text, /Local LLM · Live target · SREGym LOCAL LLM \/ LIVE TARGET · sregym 1 run · 1 scenario Resolved 1/);
  assert.match(text, /Resolved is RECAP's own oracle; SREGym's grader gives the benchmark's verdict separately \(Evaluations\)\./);
  assert.doesNotMatch(text, /[34] runs/, "no column adds the simulator and a cluster, or two clusters");
  // Each column's accessible name says source and target.
  const groups = withAttr(card, "role", "group").map((g) => g.attrs["aria-label"]);
  assert.ok(groups.includes("Local LLM · Live target · Kind (local Kubernetes)"));
  assert.ok(groups.includes("Local LLM · Simulated target"));
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
  const card = textOf(between(html, "Outcomes by data source</h2>", "Active environment</h2>"));
  assert.match(card, /Source not recorded · Target not recorded SOURCE NOT RECORDED \/ TARGET NOT RECORDED 1 run · 1 scenario Resolved 1/);
  assert.match(card, /whose decision source was not recorded/);
  assert.match(card, /These runs did not record which environment they ran against\./);
});

test("at a glance: the environment, its readiness, and how the newest runs ended, with sources counted together said plainly", () => {
  const html = overview();
  const card = textOf(between(html, "At a glance</h2>", "Recent runs</h2>"));
  assert.match(card, /Environment Simulation RECAP simulated shop · namespace shop Operational/);
  assert.match(card, /Resolved 9 The independent oracle verified the recovery\. Blocked 1 .* Failed 1 .* Active 0 Not finished yet\./);
  assert.doesNotMatch(card, /Cancelled or deferred/, "the fifth group only when it has runs");
  assert.match(card, /All 11 runs on this server\. Scripted, replayed and live-model runs are counted together here; Technical details below splits them by data source\./);
  assert.doesNotMatch(card, /%/);
  const empty = textOf(overview({ runs: polled(page([], 0)) }));
  assert.match(empty, /No incident has run on this server yet Try the guided demo to see RECAP handle one, step by step\./);
  const kind = textOf(between(clusterOverview(KIND_SYSTEM), "At a glance</h2>", "Recent runs</h2>"));
  assert.match(kind, /Environment Real cluster · Kind \(local Kubernetes\) Kind cluster recap-m5 · namespace recap-demo Operational/);
});

test("the Overview keeps every card, the technical ones in a closed Technical details section", () => {
  const html = overview();
  // The section is the page's last element (the cards inside have <details> of their own).
  const details = html.slice(html.indexOf('<details class="ov-more"'));
  assert.ok(!/<details class="ov-more"[^>]*\sopen/.test(html), "closed by default");
  for (const title of ["Start the demo", "System readiness", "Outcomes by data source", "Active environment", "Access", "Decision providers"]) {
    assert.ok(details.includes(`${title}</h2>`), title);
  }
  assert.match(textOf(details), /^Technical details System readiness, the demo recording, the environment check, outcomes by data source, access and model providers/);
});

test("the breakdown says when it counted only the newest runs", () => {
  const html = overview({ runs: polled(page(RUNS, 1234)) });
  assert.match(textOf(html), /Counted over the newest 11 of 1,234 runs\./);
  assert.match(textOf(html), /The 5 newest of 1,234 runs\./);
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
  const card = between(html, 'id="ov-active"', "Recent runs</h2>");
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
  const card = textOf(between(html, "Access</h2>", "Decision providers</h2>"));
  assert.match(card, /Reading every page Open/);
  assert.match(card, /Acting: start a run or a replay, approve or reject a fix, cancel a run, start an evaluation Operator token required/);
  assert.match(card, /This browser tab Signed out/);
  const inHtml = overview({ session: SIGNED_IN });
  assert.match(textOf(inHtml), /This browser tab Signed in as owner/);
  assert.doesNotMatch(inHtml, /Bearer/);
  // The session state is derived without ever exposing the token.
  assert.deepEqual(sessionState(null, polled(undefined)), { kind: "signed_out" });
  assert.deepEqual(sessionState("tok-secret", polled(undefined)), { kind: "checking" });
  assert.deepEqual(sessionState("tok-secret", polled({ operator: "owner" })), { kind: "signed_in", operator: "owner" });
  assert.deepEqual(sessionState("tok-secret", polled(undefined, new ApiError("Sign in", 401, "/auth/session", null, false))), { kind: "rejected" });
  assert.doesNotMatch(JSON.stringify(sessionState("tok-secret", polled({ operator: "owner" }))), /tok-secret/);
});

test("active environment on the simulator: the adapter's own facts, its scenarios, and no stale M5 card", () => {
  const html = overview();
  assert.doesNotMatch(html, /Not built yet: M5|documentation only|m5-integration-plan/);
  const card = between(html, "Active environment</h2>", "Access</h2>");
  const text = textOf(card);
  assert.match(text, /The target every incident on this server runs against \(GET \/system\)/);
  assert.match(text, /Simulated shop SIMULATED TARGET Deterministic local simulation of a five-service shop/);
  assert.match(text, /Environment RECAP simulated shop Namespace shop Adapter SimulatedEnvironment \(kind simulated \)/);
  assert.match(text, /Actions it supports rollback, restart, delete_namespace/);
  assert.match(text, /Scenarios 17 scenarios ambiguous-incident Checkout and frontend failing together/);
  assert.match(text, /Safety policy policy-v1 Recovery oracle simulated-health-oracle/);
  assert.doesNotMatch(text, /Time a fix may take to settle/, "the simulator recovers the moment a fix is applied");
  assert.match(text, /A simulated target runs inside the server: the check only confirms it\./);
  // No real-cluster guide for the simulator, and no planned list when nothing is planned.
  assert.ok(!hrefs(card).some((href) => href.includes("infra/kind") || href.includes("integrations/sregym")));
  assert.doesNotMatch(text, /Listed as planned/);
  // Scenario titles only: never a scenario's evaluation labels.
  assert.doesNotMatch(text, /family|expected/);
});

test("active environment lists what the server still calls planned, and only then", () => {
  const planned: dto.AdapterStatus = { kind: "nomad", adapter: null, configured: false, status: "planned", operational: false, milestone: "M9", detail: "no adapter in this repository" };
  const html = overview({ system: polled({ ...SYSTEM, environment_adapters: [...SYSTEM.environment_adapters, planned] }) });
  const text = textOf(between(html, "Active environment</h2>", "Access</h2>"));
  assert.match(text, /Listed as planned \(not built\) nomad adapter Planned · M9 no adapter in this repository/);
});

test("active environment: the scenarios load on their own, and say so when they cannot", () => {
  const loading = textOf(between(overview({ scenarios: polled(undefined) }), "Active environment</h2>", "Access</h2>"));
  assert.match(loading, /Scenarios Loading…/);
  const failed = textOf(between(overview({ scenarios: polled(undefined, new ApiError("boom", 500, "/scenarios", null, false)) }), "Active environment</h2>", "Access</h2>"));
  assert.match(failed, /Scenarios Not loaded \(GET \/scenarios failed\)/);
  const none = textOf(between(overview({ scenarios: polled([]) }), "Active environment</h2>", "Access</h2>"));
  assert.match(none, /Scenarios none: no incident can run here/);
});

test("on a Kind server: a live notice, a real-cluster call to action instead of the demo, and the cluster's facts", () => {
  const html = clusterOverview(KIND_SYSTEM);
  const text = textOf(html);
  // Never presented as simulated; the notice names the target.
  assert.match(text, /Live environment: Kind \(local Kubernetes\) Kind cluster recap-m5, namespace recap-demo: not simulated\. An approved action changes this real system; nothing on this page changes it\./);
  assert.doesNotMatch(text, /this page only reads|SIMULATED TARGET|SIMULATED ENVIRONMENT/);
  // No simulated demo: the hero and the first card lead to a real-cluster run.
  assert.doesNotMatch(text, /Start the demo|Open the replay panel|Try the guided demo|Interview mode/);
  assert.ok(!hrefs(html).some((href) => href.includes("panel=replay") || href.startsWith("#/demo")));
  const hero = between(html, '<section class="ov-hero"', "</section>");
  assert.match(textOf(hero), /Run a real-cluster incident/);
  assert.match(textOf(hero), /replays are refused/);
  assert.ok(hrefs(hero).includes("#/live"));
  const card = between(html, "Run a real-cluster incident</h2>", "System readiness");
  const cardText = textOf(card);
  assert.match(cardText, /Kind cluster recap-m5 · namespace recap-demo · not a simulation/);
  assert.match(cardText, /Nothing it proposes runs until a person approves that exact action\./);
  assert.match(cardText, /An approved action changes the cluster This target's actions \(rollback, restart\) each change one Deployment in namespace recap-demo \./);
  assert.match(cardText, /To approve, the operator types the target service's exact name, in the Incident Lab's approval dialog or at the console\. A rejection changes nothing\./);
  assert.match(cardText, /Starting a run already changes the cluster: RECAP's reset puts the demo app back on its baseline and applies the scenario's fault/);
  assert.match(cardText, /Starting a run needs the operator token/);
  assert.ok(hrefs(card).includes("#/live"));
  assert.ok(hrefs(card).includes(github("infra/kind/README.md")));
  // Readiness: Kind is the configured, operational adapter; the simulator and SREGym are not.
  const readiness = readinessOf(html);
  assert.match(textOf(between(readiness, "Kind (local Kubernetes)</span>", "</li>")), /configured Active Operational/);
  assert.match(textOf(between(readiness, ">Simulated shop</span>", "</li>")), /Available Not operational/);
  assert.match(textOf(between(readiness, ">SREGym</span>", "</li>")), /Available Not operational/);
  const operational = tags(between(readiness, "Kind (local Kubernetes)</span>", "</li>"), "span").find((s) => (s.attrs.title ?? "").includes("does not contact the cluster"));
  assert.ok(operational, "Operational says GET /system did not contact the cluster");
});

test("on a Kind server the active environment shows the cluster, its scenarios, settle time and guide", () => {
  const html = clusterOverview(KIND_SYSTEM);
  const card = between(html, "Active environment</h2>", "Access</h2>");
  const text = textOf(card);
  assert.match(text, /Kind \(local Kubernetes\) LIVE TARGET · kind Local Kind cluster recap-m5 \(context kind-recap-m5\), not production/);
  assert.match(text, /Environment Kind cluster recap-m5 Namespace recap-demo Adapter KubernetesEnvironment \(kind kind \)/);
  assert.match(text, /Actions it supports rollback, restart Scenarios 4 scenarios kind-bad-release Frontend errors after a backend release/);
  assert.match(text, /Safety policy policy-kind-v1 Recovery oracle kind-health-oracle Time a fix may take to settle up to 180 s, checked every 5 s/);
  assert.match(text, /GET \/system describes this target without contacting it\. Check now asks the server to reach it as a run would/);
  assert.match(text, /Nothing is changed and no run starts\./);
  assert.match(text, /Not checked from this page yet\./);
  assert.ok(hrefs(card).includes(github("infra/kind/README.md")));
  assert.doesNotMatch(text, /Listed as planned/);
});

test("on a Kind server outcomes and recent runs are labelled as live-target runs", () => {
  const html = clusterOverview(KIND_SYSTEM);
  const runs = between(html, 'class="ov-runs-list"', "</ol>");
  assert.equal(count(runs, "LIVE TARGET · kind"), 4);
  assert.equal(count(runs, "SIMULATED TARGET"), 0);
  assert.match(textOf(runs), /LOCAL LLM qwen3:8b \/ LIVE TARGET · kind/);
  const outcomes = textOf(between(html, "Outcomes by data source</h2>", "Active environment</h2>"));
  assert.match(outcomes, /Local LLM · Live target · Kind \(local Kubernetes\) LOCAL LLM \/ LIVE TARGET · kind 4 runs · 1 scenario Resolved 1 Failed 1 Approval rejected 2/);
  // The empty-folder hint points at this cluster's scenarios, not at a demo.
  const empty = textOf(clusterOverview(KIND_SYSTEM, { runs: polled(page([], 0)) }));
  assert.match(empty, /No runs yet This server's data folder holds no run yet\. Run one of this target's scenarios in the Incident Lab\./);
  assert.doesNotMatch(empty, /Start the demo/);
});

test("on a SREGym server: SREGym's harness owns the problem and its grader is separate", () => {
  const html = clusterOverview(SREGYM_SYSTEM);
  const text = textOf(html);
  assert.match(text, /Live environment: SREGym SREGym on Kind cluster kind, namespace hotel-reservation: not simulated\./);
  const card = textOf(between(html, "Run a real-cluster incident</h2>", "System readiness"));
  assert.match(card, /SREGym on Kind cluster kind · namespace hotel-reservation · not a simulation/);
  assert.match(card, /SREGym's own harness deploys the app and injects the problem before a run; RECAP's reset changes nothing\./);
  assert.match(card, /SREGym's own grader gives the benchmark's verdict separately from RECAP's oracle\./);
  assert.doesNotMatch(card, /applies the scenario's fault/);
  assert.ok(hrefs(html).includes(github("integrations/sregym/README.md")));
  const environment = textOf(between(html, "Active environment</h2>", "Access</h2>"));
  assert.match(environment, /SREGym LIVE TARGET · sregym SREGym 1\.1 on its own Kind cluster kind/);
  assert.match(environment, /Adapter SREGymEnvironment \(kind sregym \)/);
  assert.match(environment, /Scenarios 1 scenario sregym-misconfig-app-hotel-res/);
  assert.match(environment, /Safety policy policy-sregym-hotel-v2 Recovery oracle sregym-health-oracle Time a fix may take to settle up to 240 s/);
  const readiness = readinessOf(html);
  assert.match(textOf(between(readiness, ">SREGym</span>", "</li>")), /configured Active Operational/);
  assert.match(textOf(between(readiness, "Kind (local Kubernetes)</span>", "</li>")), /Available Not operational/);
});

// --- the Check now button ---------------------------------------------------------------------

function checkPanel(check: EnvironmentCheckState, session: SessionState = SIGNED_IN, kind = "kind", simulated = false): string {
  return render(<EnvironmentCheckPanel kind={kind} simulated={simulated} check={check} onCheck={() => undefined} session={session} now={NOW} />);
}

const checkButton = (html: string) => tags(html, "button")[0];

test("check now: signed out, the button waits for the operator token and nothing was checked", () => {
  const html = checkPanel(NO_CHECK, { kind: "signed_out" });
  assert.ok("disabled" in checkButton(html).attrs);
  assert.match(textOf(html), /Check now Needs the operator token \(Sign in, top right\)\. Not checked from this page yet\./);
  assert.equal(withAttr(html, "role", "alert").length, 0);
});

test("check now: signed in, the button is ready and calls the check when pressed", () => {
  const html = checkPanel(NO_CHECK);
  assert.ok(!("disabled" in checkButton(html).attrs));
  assert.equal(checkButton(html).attrs.type, "button");
  assert.doesNotMatch(textOf(html), /Needs the operator token/);
  // The button's own handler is the page's onCheck (pressed here without a browser).
  let pressed = 0;
  const tree = EnvironmentCheckPanel({ kind: "kind", simulated: false, check: NO_CHECK, onCheck: () => (pressed += 1), session: SIGNED_IN, now: NOW });
  const button = findElement(tree, (el) => el.type === "button");
  assert.ok(button, "the panel has a button");
  (button?.props as { onClick: () => void } | undefined)?.onClick();
  assert.equal(pressed, 1);
});

test("check now: while a check runs the button is busy, and an earlier answer stays visible", () => {
  const html = checkPanel({ running: true, result: null, error: null });
  assert.ok("disabled" in checkButton(html).attrs);
  assert.match(textOf(html), /Checking… Checking the target…/);
  const again = textOf(checkPanel({ running: true, result: PASSED, error: null }));
  assert.match(again, /Checking the target… Check passed/);
});

test("check now: a passed check shows each check, the target and when it ran", () => {
  const html = checkPanel({ running: false, result: PASSED, error: null });
  const text = textOf(html);
  assert.match(text, /Check passed Kind \(local Kubernetes\) \(not simulated\) · checked 12 s ago/);
  assert.match(text, /Passed preflight credentials pinned to this cluster; API server and versions answer as expected/);
  const time = tags(html, "time")[0];
  assert.equal(time.attrs.dateTime, "2026-10-26T05:29:48.000000Z");
  // The answer is announced politely; nothing is an alert.
  assert.ok(withAttr(html, "role", "status").length >= 1);
  assert.equal(withAttr(html, "role", "alert").length, 0);
  assert.doesNotMatch(text, /Not checked from this page yet/);
});

test("check now: a failed check says which check failed and why, in words", () => {
  const text = textOf(checkPanel({ running: false, result: FAILED, error: null }));
  assert.match(text, /Check failed Kind \(local Kubernetes\) \(not simulated\) · checked 12 s ago/);
  assert.match(text, /Failed preflight no answer within 20 s \(tunnel down\?\)/);
  const simulated = textOf(
    checkPanel(
      { running: false, result: { checked_at: PASSED.checked_at, environment_kind: "simulated", simulated: true, ok: true, checks: [{ name: "simulated environment", ok: true, detail: "local and in-process" }] }, error: null },
      SIGNED_IN,
      "simulated",
      true,
    ),
  );
  assert.match(simulated, /A simulated target runs inside the server: the check only confirms it\./);
  assert.match(simulated, /Check passed Simulated shop \(simulated\) · checked 12 s ago Passed simulated environment local and in-process/);
  // A live adapter this repository does not describe: no cluster credentials are claimed.
  const custom = textOf(checkPanel(NO_CHECK, SIGNED_IN, "custom.module:Adapter"));
  assert.match(custom, /Check now asks the server to run the adapter's own preflight, if it has one\./);
  assert.doesNotMatch(custom, /cluster/);
});

test("check now: 401 asks to sign in; another failure shows the server's words; nothing was changed either way", () => {
  const unauthorized = checkPanel({ running: false, result: null, error: new ApiError(SIGN_IN_HINT, 401, "/environment/check", null, false) });
  const text = textOf(unauthorized);
  assert.match(text, /Sign in to run the check · HTTP 401/);
  assert.ok(text.includes(SIGN_IN_HINT));
  assert.match(text, /No answer from the target, and nothing on it was changed\./);
  assert.equal(withAttr(unauthorized, "role", "alert").length, 1);
  assert.ok(!("disabled" in checkButton(unauthorized).attrs), "the button stays ready for after sign-in");
  const failed = textOf(checkPanel({ running: false, result: null, error: new ApiError("the environment adapter could not be built", 500, "/environment/check", null, false) }));
  assert.match(failed, /The check could not run · HTTP 500 the environment adapter could not be built No answer from the target/);
  const offline = textOf(checkPanel({ running: false, result: null, error: new ApiError("Cannot reach the RECAP API.", 0, "/environment/check", null, true) }));
  assert.match(offline, /The check could not run Cannot reach the RECAP API\. No answer from the target/);
  assert.doesNotMatch(offline, /HTTP 0/);
});

test("check now: the Overview passes its check state and session to the active environment card", () => {
  const html = clusterOverview(KIND_SYSTEM, { session: SIGNED_IN, check: { running: false, result: PASSED, error: null } });
  const card = between(html, "Active environment</h2>", "Access</h2>");
  assert.match(textOf(card), /Check passed Kind \(local Kubernetes\) \(not simulated\) · checked 12 s ago/);
  assert.ok(!("disabled" in tags(card, "button")[0].attrs));
  const signedOut = between(clusterOverview(KIND_SYSTEM), "Active environment</h2>", "Access</h2>");
  assert.ok("disabled" in tags(signedOut, "button")[0].attrs);
});

/** The first element in a returned element tree that matches (host elements only; components are not rendered). */
function findElement(node: ReactNode, match: (el: ReactElement) => boolean): ReactElement | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElement(child, match);
      if (found) return found;
    }
    return null;
  }
  if (!node || typeof node !== "object" || !("props" in node)) return null;
  const el = node as ReactElement<{ children?: ReactNode }>;
  if (match(el)) return el;
  return findElement(el.props.children, match);
}

// --- providers, states, escaping ---------------------------------------------------------------

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
  assert.match(text, /No runs yet This server's data folder holds no run yet\. Start the demo above, or run a scenario in the Incident Lab\./);
  assert.match(text, /Nothing to count yet No run has been recorded, so there is no outcome to count\./);
  const failing = new ApiError("Internal Server Error", 500, "/providers", null, false);
  const broken = overview({ providers: polled(undefined, failing), system: polled(undefined, new ApiError("boom", 500, "/system", null, false)) });
  const brokenText = textOf(broken);
  assert.match(brokenText, /Could not load GET \/providers · HTTP 500 Internal Server Error/);
  assert.match(brokenText, /Could not load GET \/system · HTTP 500 boom/);
  assert.ok(withAttr(broken, "role", "alert").length >= 4, "readiness, active environment, access and providers each report the failure");
  assert.ok(count(brokenText, "Retry") >= 4);
  // No notice is invented while /system is unknown, and no real-cluster claim either.
  assert.doesNotMatch(brokenText, /Simulated environment · this page only reads|Live environment:|Run a real-cluster incident/);
  const noProviders = overview({ providers: polled([]) });
  assert.match(textOf(noProviders), /No provider configured configs\/providers\.yaml lists no decision provider\./);
});

test("a live environment adapter is never presented as simulated, whatever its kind", () => {
  const custom = { ...KIND_SYSTEM, environment: { ...KIND_SYSTEM.environment, kind: "custom.module:Adapter", name: "a custom target" }, environment_kind: "custom.module:Adapter" };
  const html = overview({ system: polled(custom) });
  const text = textOf(html);
  assert.match(text, /Live environment: custom\.module:Adapter a custom target, namespace recap-demo: not simulated\./);
  assert.doesNotMatch(text, /this page only reads|Start the demo/);
  // It is live, but this repository does not describe it: no cluster, Deployment, guide or
  // start note is invented for it.
  assert.equal(count(text, "Run an incident on the live target"), 2, "header and card");
  assert.match(text, /An approved action changes this real system This target's actions \(rollback, restart\) change it\./);
  assert.doesNotMatch(text, /real-cluster|Deployment|Starting a run already changes|SREGym's own harness deploys/);
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

test("hostile text in the environment, its scenarios and a check answer is escaped too", () => {
  const evil = "<script>alert(1)</script>";
  const system = { ...KIND_SYSTEM, environment: { ...KIND_SYSTEM.environment, name: evil, description: '<img src=x onerror="alert(2)">', namespace: '<iframe src="y">' } };
  const html = clusterOverview(system, {
    scenarios: polled([{ id: evil, title: '<a href="javascript:alert(3)">x</a>' }]),
    check: { running: false, result: { ...FAILED, checks: [{ name: evil, ok: false, detail: '<img src=x onerror="alert(4)">' }] }, error: null },
  });
  assert.equal(tags(html, "script").length, 0);
  assert.equal(tags(html, "img").length, 0);
  assert.equal(tags(html, "iframe").length, 0);
  assert.equal(withAttr(html, "onerror").length, 0);
  assert.ok(hrefs(html).every((href) => !/^\s*javascript:/i.test(href)));
  assert.ok(textOf(html).includes(`${evil} · namespace <iframe src="y"> · not a simulation`));
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
  // Every adapter of this repository is implemented: none is drawn as planned.
  const env = between(html, 'id="amap-tab-environment"', "</button>");
  assert.match(textOf(env), /Simulated shop Active SREGym Available Kind \(local Kubernetes\) Available/);
  assert.equal(count(env, "is-planned"), 0);
  assert.match(text, /A part the server lists as planned is dashed and never shown as active\./);
  assert.doesNotMatch(text, /\(M5\)/);
});

test("the target environment's panel: adapters with their status, the targets, and what M5 verified", () => {
  const html = architecture("environment");
  const panel = between(html, 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>");
  const text = textOf(panel);
  assert.match(text, /Around every step untrusted data Target environment/);
  assert.match(text, /Simulated shop adapter configured Active Operational class SimulatedEnvironment/);
  assert.match(text, /SREGym adapter Available Not operational class SREGymEnvironment a SREGym 1\.1 problem on SREGym's own Kind cluster/);
  assert.match(text, /Kind \(local Kubernetes\) adapter Available Not operational class KubernetesEnvironment a local Kind cluster/);
  assert.match(text, /What was built and verified on each real cluster, run by run, failures included/);
  assert.match(text, /On each target Simulated shop this server A deterministic, in-process simulation/);
  assert.match(text, /Kind \(local Kubernetes\) A Kind cluster RECAP's owner controls/);
  assert.match(text, /SREGym SREGym 1\.1's own Kind cluster, whose harness deploys a benchmark application and injects the problem/);
  for (const stale of [/nothing in RECAP runs on a real cluster/, /planned for M5/, /documentation only/i, /interface only/]) assert.doesNotMatch(text, stale);
  const links = hrefs(panel);
  assert.ok(links.includes(github("docs/m5-progress.md")));
  assert.ok(links.includes(github("integrations/sregym/")));
  assert.ok(links.includes(github("recap/environments/kubernetes/")));
  assert.ok(links.includes(github("docs/decisions/0031-an-external-benchmark-harness-release-tags-and-pod-failure-onsets.md")));
  assert.ok(!links.includes(github("docs/m5-integration-plan.md")), "the panel points at what was built, not the plan");
});

test("a step's panel explains it: what it does, server facts, code, ADRs, trace events, where to see it", () => {
  const html = architecture("verifier");
  const panel = between(html, 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>");
  const text = textOf(panel);
  assert.match(text, /Step 7 of 11 trusted code Evidence verifier/);
  assert.match(text, /Status in GET \/system Verifier Active evidence-rule-verifier-v1/);
  assert.match(text, /never by votes/);
  assert.match(text, /An open disagreement sends one question back to the router \(step 4\)\./);
  assert.match(text, /This server \(GET \/system\) Verifier evidence-rule-verifier-v1/);
  assert.match(text, /In a run's trace verification\.merge verification\.evidence/);
  assert.doesNotMatch(text, /On each target/, "a step that works the same everywhere has no per-target notes");
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
  const text = textOf(between(architecture("specialists"), 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>"));
  assert.match(text, /LLMProvider Active deterministic-scripted-policy the scripted baseline: not an LLM/);
  assert.match(text, /New runs use, unless they pick another provider: SCRIPTED BASELINE deterministic-scripted-policy/);
  assert.match(text, /Providers a run may pick deterministic, ollama, replay/);
  assert.match(text, /Prompt version \(real models\) prompts-v4\.7/);
});

test("the episode panel shows which partitions each kind of run reads and writes", () => {
  const text = textOf(between(architecture("episode"), 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>"));
  assert.match(text, /Scripted baseline reads train, demo · writes demo/);
  assert.match(text, /Local LLM reads train, live · writes live/);
  assert.match(text, /Replay reads train, live · writes nothing/);
});

test("on a Kind server the flow marks Kind as this server's target, with its own probe and write limits", () => {
  const env = between(architecture("alert", polled(KIND_SYSTEM)), 'id="amap-tab-environment"', "</button>");
  assert.match(textOf(env), /Target environment untrusted data Active Kind cluster recap-m5 · kind Simulated shop Available SREGym Available Kind \(local Kubernetes\) Active/);
  const oracle = textOf(between(architecture("oracle", polled(KIND_SYSTEM)), 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>"));
  assert.match(oracle, /Recovery oracle .*RecoveryOracle Active kind-health-oracle/);
  assert.match(oracle, /On each target Simulated shop Reads the simulated shop's noise-free health/);
  assert.match(oracle, /Kind \(local Kubernetes\) this server As its own recap-oracle identity: a complete rollout, readiness, and its own HTTP probe of each service/);
  assert.equal(count(oracle, "this server"), 1);
  assert.match(oracle, /Oracle kind-health-oracle/);
  const write = textOf(between(architecture("write", polled(KIND_SYSTEM)), 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>"));
  assert.match(write, /Kind \(local Kubernetes\) this server The Kubernetes adapter verifies the capability and its own rules, then writes as recap-writer/);
  assert.match(write, /Actions it supports rollback, restart/);
  const approval = textOf(between(architecture("approval", polled(KIND_SYSTEM)), 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>"));
  assert.match(approval, /Approving needs the service's name typed yes \(not simulated\)/);
  assert.match(approval, /Kind \(local Kubernetes\) this server Only the target service's exact name approves/);
  const environment = between(architecture("environment", polled(KIND_SYSTEM)), 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>");
  assert.match(textOf(environment), /Kind \(local Kubernetes\) adapter configured Active Operational class KubernetesEnvironment/);
  assert.match(textOf(environment), /Simulated shop adapter Available Not operational/);
  assert.match(textOf(environment), /This server's target: How the Kind demo cluster, its identities and its limits are set up/);
  assert.ok(hrefs(environment).includes(github("infra/kind/README.md")));
  assert.match(textOf(environment), /Adapter class KubernetesEnvironment Namespace recap-demo Simulated no Safety policy version policy-kind-v1/);
});

test("on a SREGym server the flow says SREGym's harness owns the fault, no probe, and a separate grader", () => {
  const alert = textOf(between(architecture("alert", polled(SREGYM_SYSTEM)), 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>"));
  assert.match(alert, /SREGym this server SREGym's own harness deploys the app and injects the problem before the run\. RECAP's reset changes nothing/);
  assert.match(alert, /Environment SREGym on Kind cluster kind \(kind sregym\) Simulated no/);
  const oracle = textOf(between(architecture("oracle", polled(SREGYM_SYSTEM)), 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>"));
  assert.match(oracle, /SREGym this server No probe and no metrics there, so: a complete rollout, readiness, and containers that stayed up a minute with no exit in the last five\./);
  assert.match(oracle, /SREGym's own grader gives the benchmark's verdict separately; it is recorded by hand, never counted as RECAP's success\./);
  const write = textOf(between(architecture("write", polled(SREGYM_SYSTEM)), 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>"));
  assert.match(write, /SREGym this server .*SREGym's cluster has no admission policy, so RBAC is the only limit there\./);
  const environment = between(architecture("environment", polled(SREGYM_SYSTEM)), 'id="amap-panel"', "How a real Kubernetes action is authorized</h2>");
  assert.match(textOf(environment), /SREGym adapter configured Active Operational class SREGymEnvironment/);
  assert.match(textOf(environment), /Kind \(local Kubernetes\) adapter Available Not operational/);
  assert.ok(hrefs(environment).includes(github("integrations/sregym/README.md")));
});

test("how a real Kubernetes action is authorized: each step in order, with its code and this server's target", () => {
  const html = architecture("alert", polled(KIND_SYSTEM));
  const card = between(html, "How a real Kubernetes action is authorized</h2>", "Live wiring</h2>");
  const text = textOf(card);
  assert.match(text, /This server's target is a real cluster \(Kind cluster recap-m5, namespace recap-demo\): every approved write on it passes each step below\./);
  const titles = [
    "One exact proposal",
    "The verifier's checks",
    "A person types the service's name",
    "A signed, single-use capability",
    "The writer's limits",
    "Dry run, write, read-back, audit",
    "The independent oracle",
  ];
  let at = -1;
  for (const title of titles) {
    const next = text.indexOf(title);
    assert.ok(next > at, `${title} comes next`);
    at = next;
  }
  assert.equal(tags(card, "ol").length, 1);
  assert.equal(count(card, 'class="amap-auth-step"'), 7);
  assert.match(text, /The operator types the target service's exact name, at the console or in this dashboard/);
  assert.match(text, /On the Kind demo an admission policy also checks each patch's shape; SREGym's cluster has RBAC only\./);
  assert.match(text, /A server-side dry run first, then the write, a read-back of the Deployment, and an audit row for the run, whatever the outcome\./);
  assert.match(text, /On SREGym, SREGym's own grader gives the benchmark's verdict separately\./);
  const links = hrefs(card);
  for (const path of ["recap/environments/kubernetes/engine.py", "recap/safety/approvals.py", "recap/safety/capabilities.py", "infra/kind/manifests/", "infra/sregym/recap-rbac.yaml"]) {
    assert.ok(links.includes(github(path)), `links ${path}`);
  }
  // On the simulator the card says which steps apply there, the simulation's auto-approver included
  // (a run there may ask for it); without /system it claims nothing.
  const simulated = textOf(between(architecture("alert"), "How a real Kubernetes action is authorized</h2>", "Live wiring</h2>"));
  assert.match(
    simulated,
    /This server's target is simulated \(RECAP simulated shop\): a proposal, the verifier's checks, a person's approval \(or, in a simulation only, the auto-approver when a run asks for it\) and a signed capability apply there too, then the simulator's own write boundary changes only its own state\./,
  );
  assert.doesNotMatch(simulated, /every write gets a person's approval|a person's approval and a signed capability/);
  // A real cluster never has the auto-approver: its sentence does not offer it.
  assert.doesNotMatch(text, /auto-approver/);
  const unknown = textOf(between(architecture("alert", polled(undefined)), "How a real Kubernetes action is authorized</h2>", "Live wiring</h2>"));
  assert.doesNotMatch(unknown, /This server's target/);
  assert.match(unknown, /One exact proposal/);
  // A live adapter this repository does not describe is not told these Kubernetes steps are its own.
  const custom = { ...KIND_SYSTEM, environment: { ...KIND_SYSTEM.environment, kind: "custom.module:Adapter", name: "a custom target" }, environment_kind: "custom.module:Adapter" };
  const other = textOf(between(architecture("alert", polled(custom)), "How a real Kubernetes action is authorized</h2>", "Live wiring</h2>"));
  assert.match(other, /This server's target \(a custom target\) is not simulated, and it is not one of this repository's Kubernetes targets: the steps below describe the Kind demo and SREGym only\./);
  assert.doesNotMatch(other, /every approved write on it passes/);
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
  const learnHtml = between(html, "Learn more</h2>", "</section>");
  const learn = textOf(learnHtml);
  assert.match(learn, /End-to-end walkthrough/);
  assert.match(learn, /M5: RECAP on a real Kubernetes target \(opens on GitHub\) What was built and verified on the Kind demo cluster and on SREGym's cluster/);
  assert.match(learn, /The Kind demo cluster \(opens on GitHub\) Its demo services, RECAP's four identities, the RBAC, the admission policy and the kill switch\./);
  assert.match(learn, /The SREGym adapter \(opens on GitHub\) How RECAP works a SREGym problem/);
  assert.match(learn, /M5 integration plan \(opens on GitHub\) The design for a real Kubernetes target, Kind first, then SREGym, written before the work\./);
  assert.doesNotMatch(learn, /Documentation only/i);
  for (const path of ["docs/m5-progress.md", "infra/kind/README.md", "integrations/sregym/README.md"]) assert.ok(hrefs(learnHtml).includes(github(path)));
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
  // The learning content stays: what it does, how each target does it, code and ADRs; no
  // target is marked as this server's.
  assert.match(text, /The alert's words are data, never instructions/);
  assert.match(text, /On each target Simulated shop RECAP resets the simulated shop/);
  assert.equal(count(failed, ">this server</span>"), 0);
  assert.ok(hrefs(failed).includes("https://github.com/codeit-ronit/recap/blob/main/docs/decisions/0007-replaceable-environment-adapter.md"));
});

test("server text in the wiring is escaped", () => {
  const evil = { ...SYSTEM, components: [component("router", "RouterPolicy", "<script>alert(1)</script>", "active", '<img src=x onerror="alert(2)">')] };
  const html = architecture("routing", polled(evil));
  assert.equal(tags(html, "script").length, 0);
  assert.equal(withAttr(html, "onerror").length, 0);
  assert.ok(textOf(html).includes("<script>alert(1)</script>"));
  // The environment's own words in the authorization card are escaped too.
  const hostile = { ...KIND_SYSTEM, environment: { ...KIND_SYSTEM.environment, name: "<script>alert(3)</script>" } };
  const card = between(architecture("alert", polled(hostile)), "How a real Kubernetes action is authorized</h2>", "Live wiring</h2>");
  assert.equal(tags(card, "script").length, 0);
  assert.ok(textOf(card).includes("<script>alert(3)</script>"));
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
