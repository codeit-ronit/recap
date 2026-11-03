/**
 * Tests for src/view/system.ts: what the Overview and Architecture & Learning pages show from
 * GET /system, GET /providers and GET /runs. Run with `npm test` (Node runs TypeScript directly).
 *
 * The fixtures follow what a seeded server really returned (scripts/seed_demo.py): a simulated
 * shop that is active, SREGym and Kind planned for M5, nine scripted runs and two replays of two
 * committed recordings. Partial records (no provider class, no wall time) must read "not
 * recorded", never a default; outcomes are never added up across data sources.
 */

import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import {
  ADRS,
  ALWAYS_SHOWN,
  ARCH_NODES,
  AROUND_NODES,
  DOCS,
  FLOW_STEPS,
  NODE_ORDER,
  OUTCOME_ORDER,
  SOURCE_ORDER,
  accessFacts,
  activeRuns,
  adapterLook,
  adapterName,
  adrPath,
  archNode,
  componentById,
  componentLook,
  keyStatus,
  livenessLook,
  nextNodeId,
  nodeFacts,
  nodeStatus,
  orderProviders,
  outcomeLook,
  outcomesBySource,
  plannedGaps,
  providerReadiness,
  providerSource,
  repoUrl,
  runDuration,
  scanNote,
  selectableLook,
  selectedNodeId,
  sessionLook,
  sourceOf,
  tallyComponents,
} from "../src/view/system.ts";
import type { AdapterStatus, ComponentStatus, RunSummary, SystemView } from "../src/dto.ts";

// --- fixtures ---------------------------------------------------------------------------

function component(id: string, iface: string, implementation: string, status: ComponentStatus["status"], detail = ""): ComponentStatus {
  return { id, interface: iface, implementation, status, detail };
}

const COMPONENTS: ComponentStatus[] = [
  component("environment", "TargetEnvironment", "RECAP simulated shop (kind simulated)", "active", "17 scenarios; simulated"),
  component("tool_transport", "ToolTransport", "MCP over stdio (official Python SDK, subprocess per run)", "active"),
  component("provider", "LLMProvider", "deterministic-scripted-policy", "active", "the scripted baseline: not an LLM"),
  component("retriever", "RetrievalIndex", "BM25 keyword search with a lexical reranker", "active"),
  component("hybrid_retrieval", "Embedder", "ollama:nomic-embed-text (local, loopback) + reciprocal rank fusion", "available"),
  component("memory", "ExperienceStore", "SQLite episodes, consolidator v2 (deterministic extraction)", "active", "8 episodes"),
  component("router", "RouterPolicy", "rules-router-v1", "active"),
  component("safety_policy", "SafetyPolicy", "recap-policy-engine (policy-v1)", "active"),
  component("approvals", "ApprovalStore", "exact, single-use approvals bound to a proposal digest", "active"),
  component("write_boundary", "TargetEnvironment.execute_authorized", "Ed25519-signed, single-use capabilities", "active"),
  component("verifier", "Verifier", "evidence-rule-verifier-v1", "active"),
  component("oracle", "RecoveryOracle", "simulated-health-oracle", "active"),
  component("trace_sinks", "TraceSink", "sqlite, jsonl, live-stream", "active"),
  component("opentelemetry", "TraceSink", "OpenTelemetry console spans", "disabled", "optional: RECAP_ENABLE_OTEL=1"),
];

const ADAPTERS: AdapterStatus[] = [
  {
    kind: "simulated",
    adapter: "SimulatedEnvironment",
    configured: true,
    status: "active",
    operational: true,
    milestone: null,
    detail: "deterministic local simulation of a five-service shop (recap/environments/simulated/)",
  },
  {
    kind: "sregym",
    adapter: "SREGymEnvironment",
    configured: false,
    status: "planned",
    operational: false,
    milestone: "M5",
    detail: "interface only: every operation raises EnvironmentNotIntegrated (integrations/sregym/); the real integration is planned for M5",
  },
  {
    kind: "kind",
    adapter: null,
    configured: false,
    status: "planned",
    operational: false,
    milestone: "M5",
    detail: "no adapter in this repository: a local Kind cluster is the first M5 target",
  },
];

const SYSTEM = {
  schema_version: "recap-api-v1",
  environment: {
    kind: "simulated",
    name: "RECAP simulated shop",
    namespace: "shop",
    is_simulated: true,
    supported_actions: ["rollback", "restart", "delete_namespace"],
    description: "Deterministic local simulation of a five-service shop.",
    stabilization_timeout_seconds: 0,
    stabilization_poll_seconds: 2,
  },
  environment_kind: "simulated",
  tool_transport: "MCP over stdio (official Python SDK, subprocess per run)",
  provider: { name: "deterministic-scripted-policy", kind: "scripted", origin: "scripted", model: null },
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
  simulation_notice: "SIMULATED ENVIRONMENT: every incident runs against RECAP's local simulation of a five-service shop.",
  components: COMPONENTS,
  environment_adapters: ADAPTERS,
  features: {
    retrieval_default: "bm25",
    hybrid_ready: true,
    embeddings_enabled: false,
    vector_index: { embedder: "ollama:nomic-embed-text", dimensions: 768 },
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
} as unknown as SystemView;

type RunBits = Pick<RunSummary, "provider_class" | "provider_kind" | "outcome" | "scenario_id" | "replay" | "terminal" | "wall_seconds" | "status">;

function run(over: Partial<RunBits>): RunBits {
  return {
    provider_class: "scripted",
    provider_kind: "scripted",
    outcome: "resolved",
    scenario_id: "bad-deployment",
    replay: null,
    terminal: true,
    wall_seconds: 0.84,
    status: "finished",
    ...over,
  };
}

function replayOf(fixture: string, recording: string): RunSummary["replay"] {
  return {
    fixture_id: fixture,
    recording_id: recording,
    recording_matches_fixture: true,
    committed_fixture: true,
    original_provider: "ollama",
    model: "qwen3:8b",
    prompt_version: "prompts-v4.7",
    recorded_at: "2026-10-25T12:18:28.811038+00:00",
    fixture_title: "Unfamiliar incident, two specialists, owner-approved fix (M3 deliverable)",
    fixture_sha256: "37830779420b",
    original_export: null,
    originally_approved_by: "the owner",
  };
}

/** The seeded data folder: 9 scripted runs and 2 replays of 2 recordings. */
const SEEDED: RunBits[] = [
  run({ provider_class: "replay", provider_kind: "replay", replay: replayOf("qwen3-8b-bad-deployment", "20261025-110603-ollama-b1ae62") }),
  run({ provider_class: "replay", provider_kind: "replay", scenario_id: "m3-inventory-leak-behind-frontend", replay: replayOf("qwen3-8b-unfamiliar-parallel", "20261025-121828-ollama-6a5cfd") }),
  run({ scenario_id: "m3-payment-release-under-forged-signals" }),
  run({ scenario_id: "m3-inventory-leak-behind-frontend" }),
  run({ scenario_id: "budget-exhausted", outcome: "budget_exhausted" }),
  run({ scenario_id: "malicious-log", outcome: "safety_blocked" }),
  run({ scenario_id: "memory-caution" }),
  run({ scenario_id: "ambiguous-incident" }),
  run({ scenario_id: "bad-deployment" }),
  run({ scenario_id: "train-checkout-bad-release" }),
  run({ scenario_id: "train-inventory-leak" }),
];

// --- components, adapters, gaps -----------------------------------------------------------

test("component states read as words, with a distinct icon per meaning", () => {
  assert.equal(componentLook("active").label, "Active");
  assert.equal(componentLook("available").label, "Available");
  assert.equal(componentLook("planned").label, "Planned");
  assert.equal(componentLook("error").tone, "bad");
  const icons = new Set(["active", "available", "unavailable", "disabled", "planned", "error"].map((s) => componentLook(s).icon));
  assert.equal(icons.size, 6, "each state has its own icon shape (never color alone)");
  // An unknown or missing state is never shown as active.
  assert.equal(componentLook("warming_up").label, "Warming up");
  assert.equal(componentLook("warming_up").tone, "neutral");
  assert.equal(componentLook(null).label, "Not reported");
});

test("a planned adapter names its milestone and is never active", () => {
  const [simulated, sregym, kind] = ADAPTERS;
  assert.equal(adapterLook(simulated).label, "Active");
  assert.equal(adapterLook(sregym).label, "Planned · M5");
  assert.equal(adapterLook(kind).label, "Planned · M5");
  assert.notEqual(adapterLook(kind).tone, "ok");
  assert.equal(adapterLook({ status: "planned", milestone: null }).label, "Planned");
  assert.equal(adapterName("kind"), "Kind (local Kubernetes)");
  assert.equal(adapterName("sregym"), "SREGym");
  assert.equal(adapterName("custom.module:Adapter"), "custom.module:Adapter");
});

test("components are tallied by status and the inactive ones are listed", () => {
  const tally = tallyComponents(COMPONENTS);
  assert.deepEqual(tally.counts, [
    { state: "active", count: 12 },
    { state: "available", count: 1 },
    { state: "disabled", count: 1 },
  ]);
  assert.deepEqual(
    tally.notActive.map((c) => c.id),
    ["hybrid_retrieval", "opentelemetry"],
  );
  assert.deepEqual(tallyComponents([]), { counts: [], notActive: [] });
});

test("M5 gaps are exactly what the server lists as planned", () => {
  const gaps = plannedGaps(SYSTEM);
  assert.deepEqual(
    gaps.map((g) => g.id),
    ["adapter:sregym", "adapter:kind"],
  );
  // An evaluation mode the server lists as planned is a gap too (since M5 none is).
  const planned = { ...SYSTEM.features, evaluation_modes: { ...SYSTEM.features.evaluation_modes, real_kubernetes: "planned" } };
  assert.ok(plannedGaps({ ...SYSTEM, features: planned }).some((g) => g.id === "eval:real_kubernetes"));
  assert.equal(gaps[1].title, "Kind (local Kubernetes) adapter");
  assert.equal(gaps[1].look.label, "Planned · M5");
  assert.match(gaps[0].detail, /EnvironmentNotIntegrated/);
  // A planned component is a gap too; an active adapter never is.
  const more = plannedGaps({ ...SYSTEM, components: [...COMPONENTS, component("k8s_probe", "RecoveryOracle", "kubectl rollout status", "planned", "M5")] });
  assert.ok(more.some((g) => g.id === "component:k8s_probe"));
  assert.ok(!more.some((g) => g.id === "adapter:simulated"));
  // Nothing planned: an empty list, not an invented one.
  assert.deepEqual(
    plannedGaps({ ...SYSTEM, environment_adapters: [ADAPTERS[0]], features: { ...SYSTEM.features, evaluation_modes: {} } }),
    [],
  );
});

test("componentById finds a component or says it is not listed", () => {
  assert.equal(componentById(SYSTEM, "router")?.implementation, "rules-router-v1");
  assert.equal(componentById(SYSTEM, "controller"), null);
  assert.equal(componentById(null, "router"), null);
});

// --- providers ------------------------------------------------------------------------------

test("provider readiness, selection and keys are words; a key is never more than set or missing", () => {
  assert.equal(providerReadiness("ready").label, "Ready");
  assert.equal(providerReadiness("needs_setup").label, "Needs setup");
  assert.equal(providerReadiness("stopped").tone, "bad");
  assert.equal(providerReadiness("disabled").label, "Disabled");
  assert.equal(providerReadiness("something_new").label, "Something new");
  assert.equal(selectableLook(true).label, "Selectable per run");
  assert.equal(selectableLook(false).label, "Not selectable");
  assert.equal(keyStatus({ key_required: false, key_present: null }), null);
  assert.equal(keyStatus({ key_required: true, key_present: true }), "API key set (its value is never shown)");
  assert.equal(keyStatus({ key_required: true, key_present: false }), "API key missing");
  assert.equal(keyStatus({ key_required: true, key_present: null }), "API key not checked");
});

test("providers are ordered default first, then by class", () => {
  const list = [
    { name: "anthropic", default: false, provider_class: "paid_llm" as const },
    { name: "groq", default: false, provider_class: "free_hosted_llm" as const },
    { name: "ollama", default: false, provider_class: "local_llm" as const },
    { name: "replay", default: false, provider_class: "replay" as const },
    { name: "deterministic", default: true, provider_class: "scripted" as const },
  ];
  assert.deepEqual(
    orderProviders(list).map((p) => p.name),
    ["deterministic", "replay", "ollama", "groq", "anthropic"],
  );
});

test("providerSource keeps only known fields of GET /system provider", () => {
  assert.deepEqual(providerSource({ name: "deterministic-scripted-policy", kind: "scripted", origin: "scripted", model: null, server: { x: 1 } }), {
    name: "deterministic-scripted-policy",
    origin: "scripted",
    kind: "scripted",
    model: null,
    error: null,
  });
  const broken = providerSource({ name: "groq", error: "GROQ_API_KEY is not set", origin: "made_up" });
  assert.equal(broken.origin, undefined, "an unknown origin is dropped, never passed on");
  assert.equal(broken.error, "GROQ_API_KEY is not set");
  assert.deepEqual(providerSource(null), { name: null, origin: undefined, kind: undefined, model: null, error: null });
});

// --- runs: data source, outcomes, duration, liveness --------------------------------------

test("a run's data source comes from what was recorded, never from its name", () => {
  assert.equal(sourceOf({ provider_class: "local_llm", provider_kind: "llm" }), "local_llm");
  assert.equal(sourceOf({ provider_class: null, provider_kind: "scripted" }), "scripted");
  assert.equal(sourceOf({ provider_class: null, provider_kind: "replay" }), "replay");
  // An LLM run recorded without its class cannot be called local or hosted.
  assert.equal(sourceOf({ provider_class: null, provider_kind: "llm" }), "unknown");
  assert.equal(sourceOf({ provider_class: null, provider_kind: null }), "unknown");
});

test("outcomes are counted per data source and never added together", () => {
  const groups = outcomesBySource(SEEDED);
  assert.deepEqual(
    groups.map((g) => g.source),
    ["scripted", "replay", "local_llm", "free_hosted_llm"],
  );
  const [scripted, replay, local, hosted] = groups;
  assert.equal(scripted.total, 9);
  assert.deepEqual(scripted.outcomes, [
    { outcome: "resolved", count: 7 },
    { outcome: "safety_blocked", count: 1 },
    { outcome: "budget_exhausted", count: 1 },
  ]);
  assert.equal(scripted.resolved, 7);
  assert.equal(scripted.scenarios, 9);
  assert.equal(scripted.recordings, null);
  assert.equal(replay.total, 2);
  assert.equal(replay.recordings, 2, "two replays of two different recordings");
  assert.deepEqual(local, { source: "local_llm", total: 0, outcomes: [], resolved: 0, inProgress: 0, scenarios: 0, recordings: null });
  assert.equal(hosted.total, 0);
  // No group, and no field, adds sources together.
  for (const group of groups) {
    assert.deepEqual(Object.keys(group).sort(), ["inProgress", "outcomes", "recordings", "resolved", "scenarios", "source", "total"]);
  }
});

test("replaying one recording again counts as one recording", () => {
  const again = replayOf("qwen3-8b-unfamiliar-parallel", "20261025-121828-ollama-6a5cfd");
  const [, replay] = outcomesBySource([run({ provider_class: "replay", replay: again }), run({ provider_class: "replay", replay: again })]);
  assert.equal(replay.total, 2);
  assert.equal(replay.recordings, 1);
});

test("unusual sources and outcomes appear only when present, in a fixed order", () => {
  const groups = outcomesBySource([
    run({ provider_class: null, provider_kind: null, outcome: "failed" }),
    run({ provider_class: "paid_llm", provider_kind: "llm", outcome: "error" }),
    run({ provider_class: "local_llm", provider_kind: "llm", outcome: "quota_deferred" }),
    run({ provider_class: "local_llm", provider_kind: "llm", outcome: "in_progress", terminal: false }),
    run({ provider_class: "local_llm", provider_kind: "llm", outcome: "brand_new_outcome" }),
  ]);
  assert.deepEqual(
    groups.map((g) => g.source),
    ["scripted", "replay", "local_llm", "free_hosted_llm", "paid_llm", "unknown"],
  );
  const local = groups[2];
  assert.deepEqual(
    local.outcomes.map((o) => o.outcome),
    ["quota_deferred", "in_progress", "brand_new_outcome"],
  );
  assert.equal(local.inProgress, 1);
  assert.ok(SOURCE_ORDER.every((s) => s === "paid_llm" || s === "unknown" || ALWAYS_SHOWN.has(s)));
  assert.equal(OUTCOME_ORDER[0], "resolved");
});

test("outcome labels: resolved means the oracle verified it; unknown outcomes are shown as sent", () => {
  assert.equal(outcomeLook("resolved").label, "Resolved");
  assert.match(outcomeLook("resolved").title, /oracle/);
  assert.equal(outcomeLook("safety_blocked").label, "Safety blocked");
  assert.equal(outcomeLook("approval_rejected").label, "Approval rejected");
  assert.equal(outcomeLook("in_progress").icon, "live");
  assert.equal(outcomeLook("brand_new_outcome").label, "Brand new outcome");
  assert.equal(outcomeLook(null).label, "Not recorded");
});

test("a duration is measured, still running, or not recorded: never a guess", () => {
  assert.deepEqual(runDuration({ wall_seconds: 0.842, terminal: true }), { kind: "measured", ms: 842 });
  assert.deepEqual(runDuration({ wall_seconds: null, terminal: false }), { kind: "running" });
  assert.deepEqual(runDuration({ wall_seconds: null, terminal: true }), { kind: "not_recorded" });
  assert.deepEqual(runDuration({ wall_seconds: Number.NaN, terminal: true }), { kind: "not_recorded" });
  assert.deepEqual(runDuration({ wall_seconds: -1, terminal: true }), { kind: "not_recorded" });
});

test("active incidents are the unfinished runs, and liveness says whether this server can follow them", () => {
  const list = [run({ terminal: false, status: "running", outcome: "in_progress" }), run({}), run({ terminal: false, status: "external", outcome: "in_progress" })];
  assert.equal(activeRuns(list).length, 2);
  assert.equal(livenessLook("running").label, "Running in this server");
  assert.equal(livenessLook("external").label, "Not running in this server");
  assert.equal(livenessLook("error").tone, "bad");
  assert.equal(livenessLook("unknown").label, "Status unknown");
});

test("the breakdown says when it counted only the newest runs", () => {
  assert.equal(scanNote(11, 11), null);
  assert.equal(scanNote(500, null), null);
  assert.equal(scanNote(500, 1234), "Counted over the newest 500 of 1,234 runs.");
});

// --- access ---------------------------------------------------------------------------------

test("access reads the server's rules; the session never carries the token", () => {
  const facts = accessFacts(SYSTEM.features);
  assert.equal(facts.reads.label, "Open");
  assert.equal(facts.writes.label, "Operator token required");
  assert.equal(accessFacts({ reads_require_token: true, writes_require_operator_token: false }).writes.tone, "bad");
  assert.equal(sessionLook({ kind: "signed_out" }).label, "Signed out");
  assert.equal(sessionLook({ kind: "signed_in", operator: "owner" }).label, "Signed in as owner");
  assert.equal(sessionLook({ kind: "rejected" }).label, "Token rejected");
  assert.equal(sessionLook({ kind: "checking" }).label, "Checking the sign-in");
  assert.equal(sessionLook({ kind: "unverified", message: "Cannot reach the RECAP API." }).title, "Cannot reach the RECAP API.");
});

// --- the flow map ---------------------------------------------------------------------------

test("the flow has the eleven steps of a run, in order, then the parts around them", () => {
  assert.deepEqual(
    FLOW_STEPS.map((n) => n.id),
    ["alert", "controller", "retrieval", "routing", "specialists", "gateway", "verifier", "approval", "write", "oracle", "episode"],
  );
  assert.deepEqual(
    FLOW_STEPS.map((n) => n.step),
    [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
  );
  assert.deepEqual(
    AROUND_NODES.map((n) => n.id),
    ["environment", "telemetry"],
  );
  assert.equal(NODE_ORDER.length, ARCH_NODES.length);
  assert.equal(new Set(NODE_ORDER).size, NODE_ORDER.length);
});

test("arrow keys move through the steps and wrap; Home and End jump", () => {
  assert.equal(nextNodeId("alert", "ArrowDown"), "controller");
  assert.equal(nextNodeId("alert", "ArrowRight"), "controller");
  assert.equal(nextNodeId("alert", "ArrowUp"), "telemetry");
  assert.equal(nextNodeId("telemetry", "ArrowDown"), "alert");
  assert.equal(nextNodeId("episode", "ArrowDown"), "environment");
  assert.equal(nextNodeId("verifier", "Home"), "alert");
  assert.equal(nextNodeId("verifier", "End"), "telemetry");
  assert.equal(nextNodeId("verifier", "Enter"), null);
  assert.equal(nextNodeId("verifier", "a"), null);
});

test("a deep link selects a step; anything else selects the first", () => {
  assert.equal(selectedNodeId("verifier"), "verifier");
  assert.equal(selectedNodeId("<script>"), "alert");
  assert.equal(selectedNodeId(null), "alert");
  assert.equal(archNode("nope"), null);
});

test("a step's status is its first /system component's; the controller has none", () => {
  assert.equal(nodeStatus(archNode("controller")!, SYSTEM), null);
  assert.equal(nodeStatus(archNode("retrieval")!, SYSTEM)?.look.label, "Active");
  assert.equal(nodeStatus(archNode("telemetry")!, SYSTEM)?.component?.implementation, "sqlite, jsonl, live-stream");
  // A component this server does not list reads "Not reported", never active.
  const missing = nodeStatus(archNode("oracle")!, { components: COMPONENTS.filter((c) => c.id !== "oracle") });
  assert.equal(missing?.component, null);
  assert.equal(missing?.look.label, "Not reported");
  // A planned write boundary (an adapter without writes) is planned, never active.
  const planned = nodeStatus(archNode("write")!, {
    components: COMPONENTS.map((c) => (c.id === "write_boundary" ? { ...c, status: "planned" as const } : c)),
  });
  assert.equal(planned?.look.label, "Planned");
});

test("every step maps to /system components that exist, ADRs that exist and repo paths", () => {
  const ids = new Set(COMPONENTS.map((c) => c.id));
  for (const node of ARCH_NODES) {
    for (const id of node.components) assert.ok(ids.has(id), `${node.id}: unknown component ${id}`);
    for (const part of node.parts) if (part.component) assert.ok(ids.has(part.component), `${node.id}: unknown part ${part.component}`);
    for (const adr of node.adrs) assert.ok(ADRS.has(adr), `${node.id}: unknown ADR ${adr}`);
    assert.ok(node.what.length >= 1 && node.modules.length >= 1 && node.adrs.length >= 1, `${node.id} explains itself`);
    for (const path of node.modules) assert.ok(!path.startsWith("/") && !path.includes(".."), `${node.id}: ${path} is repo-relative`);
  }
});

test("every linked code path, ADR and document exists in this repository", () => {
  const repo = fileURLToPath(new URL("../../../", import.meta.url));
  const paths = new Set<string>([
    ...ARCH_NODES.flatMap((n) => n.modules),
    ...[...ADRS.values()].map(adrPath),
    ...Object.values(DOCS),
  ]);
  for (const path of paths) assert.ok(existsSync(repo + path), `missing in the repository: ${path}`);
});

test("repository links point at GitHub: blob for files, tree for folders", () => {
  assert.equal(repoUrl("docs/flow.md"), "https://github.com/codeit-ronit/recap/blob/main/docs/flow.md");
  assert.equal(repoUrl("recap/providers/"), "https://github.com/codeit-ronit/recap/tree/main/recap/providers");
  assert.equal(repoUrl("/docs/flow.md"), "https://github.com/codeit-ronit/recap/blob/main/docs/flow.md");
});

test("node facts are read from GET /system", () => {
  const facts = (id: string) => Object.fromEntries(nodeFacts(archNode(id)!, SYSTEM));
  assert.equal(facts("retrieval")["Default search"], "bm25");
  assert.equal(facts("retrieval")["Hybrid search can run"], "yes");
  assert.equal(facts("retrieval")["Agent searches per run"], "6");
  assert.equal(facts("specialists")["Default provider"], "deterministic-scripted-policy");
  assert.equal(facts("specialists")["Providers a run may pick"], "deterministic, ollama, replay");
  assert.equal(facts("episode")["Replay"], "reads train, live · writes nothing");
  assert.equal(facts("episode")["Scripted baseline"], "reads train, demo · writes demo");
  assert.equal(facts("alert")["Simulated"], "yes");
  assert.equal(facts("approval")["Deciding needs the operator token"], "yes");
  assert.equal(facts("telemetry")["OpenTelemetry"], "off");
  // A server with no stored vectors says so instead of inventing an embedder.
  const bare = Object.fromEntries(nodeFacts(archNode("retrieval")!, { ...SYSTEM, embedder: null }));
  assert.equal(bare["Stored vectors"], "Not recorded");
});
