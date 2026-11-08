/**
 * Tests for src/view/system.ts: what the Overview and Architecture & Learning pages show from
 * GET /system, GET /providers and GET /runs. Run with `npm test` (Node runs TypeScript directly).
 *
 * The fixtures follow what servers of this repository really return: the seeded simulated
 * server (scripts/seed_demo.py: the simulated shop active, the Kind and SREGym adapters
 * implemented but not configured, nine scripted runs and two replays of two committed
 * recordings), and the adapter lists of a server with RECAP_TARGET=kind-demo or sregym-hotel
 * (Runtime.system_summary, offline). Partial records (no provider class, no target, no wall
 * time) must read "not recorded", never a default; outcomes are never added up across data
 * sources or targets.
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
  KUBERNETES_AUTHORIZATION,
  NODE_ORDER,
  OUTCOME_ORDER,
  SOURCE_NOTES,
  SOURCE_ORDER,
  TARGET_KINDS,
  accessFacts,
  GLANCE_GROUPS,
  activeRuns,
  adapterLook,
  adapterName,
  adrPath,
  archNode,
  checkLook,
  componentById,
  componentLook,
  configuredAdapter,
  glanceCounts,
  keyStatus,
  livenessLook,
  nextNodeId,
  nodeFacts,
  nodeStatus,
  operationalLook,
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
  targetGuide,
  targetLabel,
  targetNote,
  targetOf,
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

const SIMULATED_DETAIL = "deterministic local simulation of a five-service shop (recap/environments/simulated/)";
const SREGYM_DETAIL =
  "a SREGym 1.1 problem on SREGym's own Kind cluster, deployed and injected by its external harness: RECAP's Kubernetes adapter with image-tag releases and no probe or metrics, three RBAC-limited identities of its own, and SREGym's mitigation oracle as the benchmark's grader (integrations/sregym/, ADR 0031); chosen with RECAP_TARGET=sregym-hotel. GET /system never contacts the cluster";
const KIND_DETAIL =
  "a local Kind cluster through typed kubectl operations, four RBAC-limited identities and an HTTP probe of its own (recap/environments/kubernetes/); chosen with RECAP_TARGET=kind-demo. GET /system never contacts the cluster: whether it is up is checked when a run starts";

/** GET /system `environment_adapters` with `configured` the active one (recap/runtime.py). */
function adaptersFor(configured: "simulated" | "kind" | "sregym"): AdapterStatus[] {
  const entry = (kind: string, adapter: string, detail: string): AdapterStatus => ({
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

const ADAPTERS = adaptersFor("simulated");

/** An adapter a server lists as not built (none does since M5; a later one might). */
const PLANNED: AdapterStatus = {
  kind: "nomad",
  adapter: null,
  configured: false,
  status: "planned",
  operational: false,
  milestone: "M9",
  detail: "no adapter in this repository",
};

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

/** The same server with RECAP_TARGET=kind-demo (what Runtime.system_summary reports, offline). */
const KIND_SYSTEM: SystemView = {
  ...SYSTEM,
  environment: {
    kind: "kind",
    name: "Kind cluster recap-m5",
    namespace: "recap-demo",
    is_simulated: false,
    supported_actions: ["rollback", "restart"],
    description: "Local Kind cluster recap-m5 (context kind-recap-m5), not production: 2 demo services in namespace recap-demo.",
    stabilization_timeout_seconds: 180,
    stabilization_poll_seconds: 5,
  },
  environment_kind: "kind",
  oracle: "kind-health-oracle",
  safety_policy: "recap-policy-engine (policy-kind-v1)",
  label: "LIVE ENVIRONMENT (kind) · V1",
  simulation_notice: null,
  environment_adapters: adaptersFor("kind"),
  versions: { ...SYSTEM.versions, policy_version: "policy-kind-v1" },
};

/** The same server with RECAP_TARGET=sregym-hotel. */
const SREGYM_SYSTEM: SystemView = {
  ...KIND_SYSTEM,
  environment: {
    ...KIND_SYSTEM.environment,
    kind: "sregym",
    name: "SREGym on Kind cluster kind",
    namespace: "hotel-reservation",
    description: "SREGym 1.1 on its own Kind cluster kind (context kind-kind), not production.",
    stabilization_timeout_seconds: 240,
  },
  environment_kind: "sregym",
  oracle: "sregym-health-oracle",
  safety_policy: "recap-policy-engine (policy-sregym-hotel-v2)",
  label: "LIVE ENVIRONMENT (sregym) · V1",
  environment_adapters: adaptersFor("sregym"),
  versions: { ...SYSTEM.versions, policy_version: "policy-sregym-hotel-v2" },
};

type RunBits = Pick<
  RunSummary,
  "provider_class" | "provider_kind" | "outcome" | "scenario_id" | "replay" | "terminal" | "wall_seconds" | "status" | "environment_kind" | "environment_simulated"
>;

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
    environment_kind: "simulated",
    environment_simulated: true,
    ...over,
  };
}

/** A run on a real cluster with the self-hosted model (provider ollama-vm). */
function clusterRun(kind: "kind" | "sregym", over: Partial<RunBits> = {}): RunBits {
  return run({ provider_class: "local_llm", provider_kind: "llm", environment_kind: kind, environment_simulated: false, scenario_id: `${kind}-bad-release`, ...over });
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

test("every M5 adapter is implemented: the configured one is active, the others available; a planned one names its milestone", () => {
  const [simulated, sregym, kind] = ADAPTERS;
  assert.equal(adapterLook(simulated).label, "Active");
  assert.equal(adapterLook(sregym).label, "Available");
  assert.equal(adapterLook(kind).label, "Available");
  const onKind = adaptersFor("kind");
  assert.deepEqual(
    onKind.map((a) => adapterLook(a).label),
    ["Available", "Available", "Active"],
  );
  // A planned adapter (none since M5) is never active and names its milestone.
  assert.equal(adapterLook(PLANNED).label, "Planned · M9");
  assert.notEqual(adapterLook(PLANNED).tone, "ok");
  assert.equal(adapterLook({ status: "planned", milestone: null }).label, "Planned");
  assert.equal(adapterName("kind"), "Kind (local Kubernetes)");
  assert.equal(adapterName("sregym"), "SREGym");
  assert.equal(adapterName("custom.module:Adapter"), "custom.module:Adapter");
});

test("only the configured adapter is operational; on a real cluster that is where runs go, not that it answers", () => {
  assert.equal(configuredAdapter(SYSTEM)?.kind, "simulated");
  assert.equal(configuredAdapter(KIND_SYSTEM)?.adapter, "KubernetesEnvironment");
  assert.equal(configuredAdapter(SREGYM_SYSTEM)?.adapter, "SREGymEnvironment");
  assert.equal(configuredAdapter({ environment_adapters: [] }), null);

  const [simulated, sregym, kind] = adaptersFor("kind");
  assert.equal(operationalLook(kind).label, "Operational");
  assert.equal(operationalLook(kind).tone, "ok");
  assert.match(operationalLook(kind).title, /does not contact the cluster: Check now/);
  assert.equal(operationalLook(simulated).label, "Not operational");
  assert.equal(operationalLook(sregym).label, "Not operational");
  assert.notEqual(operationalLook(sregym).tone, "ok");
  // The simulator, when configured, needs no such caveat.
  assert.doesNotMatch(operationalLook(adaptersFor("simulated")[0]).title, /cluster/);
});

test("each real-cluster target links where it is set up; the simulator has no such guide", () => {
  assert.deepEqual(targetGuide("kind")?.path, "infra/kind/README.md");
  assert.deepEqual(targetGuide("sregym")?.path, "integrations/sregym/README.md");
  assert.equal(targetGuide("simulated"), null);
  assert.equal(targetGuide("custom.module:Adapter"), null);
  assert.equal(targetGuide(null), null);
  assert.deepEqual([...TARGET_KINDS], ["simulated", "kind", "sregym"]);
});

test("a readiness check reads passed or failed in words, and says nothing was changed", () => {
  assert.equal(checkLook(true).label, "Passed");
  assert.equal(checkLook(true).tone, "ok");
  assert.equal(checkLook(false).label, "Failed");
  assert.equal(checkLook(false).tone, "bad");
  assert.notEqual(checkLook(true).icon, checkLook(false).icon, "never color alone");
  for (const ok of [true, false]) assert.match(checkLook(ok).title, /Nothing was changed/);
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

test("planned items are exactly what the server lists as planned: an M5 server lists none", () => {
  // Kind and SREGym are implemented, whichever target is configured: nothing is planned.
  for (const system of [SYSTEM, KIND_SYSTEM, SREGYM_SYSTEM]) assert.deepEqual(plannedGaps(system), []);
  // A planned adapter, component or evaluation mode is listed with the server's own words.
  const gaps = plannedGaps({ ...SYSTEM, environment_adapters: [...ADAPTERS, PLANNED] });
  assert.deepEqual(
    gaps.map((g) => g.id),
    ["adapter:nomad"],
  );
  assert.equal(gaps[0].title, "nomad adapter");
  assert.equal(gaps[0].look.label, "Planned · M9");
  assert.equal(gaps[0].detail, "no adapter in this repository");
  const planned = { ...SYSTEM.features, evaluation_modes: { ...SYSTEM.features.evaluation_modes, real_kubernetes: "planned" } };
  assert.ok(plannedGaps({ ...SYSTEM, features: planned }).some((g) => g.id === "eval:real_kubernetes"));
  const more = plannedGaps({ ...SYSTEM, components: [...COMPONENTS, component("k8s_probe", "RecoveryOracle", "kubectl rollout status", "planned", "M9")] });
  assert.ok(more.some((g) => g.id === "component:k8s_probe"));
  // An implemented adapter is never a gap, configured or not.
  assert.ok(!more.some((g) => g.id.startsWith("adapter:")));
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
  assert.deepEqual(scripted.target, { key: "simulated", simulated: true, kind: "simulated" });
  assert.deepEqual(local, { source: "local_llm", target: null, total: 0, outcomes: [], resolved: 0, inProgress: 0, scenarios: 0, recordings: null });
  assert.equal(hosted.total, 0);
  // No group, and no field, adds sources together.
  for (const group of groups) {
    assert.deepEqual(Object.keys(group).sort(), ["inProgress", "outcomes", "recordings", "resolved", "scenarios", "source", "target", "total"]);
  }
});

test("a run's target comes from what it recorded: simulated, a real cluster's kind, or not recorded", () => {
  assert.deepEqual(targetOf({ environment_simulated: true, environment_kind: "simulated" }), { key: "simulated", simulated: true, kind: "simulated" });
  assert.deepEqual(targetOf({ environment_simulated: false, environment_kind: "kind" }), { key: "live:kind", simulated: false, kind: "kind" });
  assert.deepEqual(targetOf({ environment_simulated: false, environment_kind: "sregym" }), { key: "live:sregym", simulated: false, kind: "sregym" });
  // Not simulated, kind missing: still live, never guessed to be one cluster or another.
  assert.deepEqual(targetOf({ environment_simulated: false, environment_kind: null }), { key: "live:", simulated: false, kind: null });
  // A kind without the simulated flag says nothing about where the run acted.
  assert.deepEqual(targetOf({ environment_simulated: null, environment_kind: "kind" }), { key: "unknown", simulated: null, kind: null });
  assert.equal(targetLabel(targetOf({ environment_simulated: true, environment_kind: "simulated" })), "Simulated target");
  assert.equal(targetLabel(targetOf({ environment_simulated: false, environment_kind: "kind" })), "Live target · Kind (local Kubernetes)");
  assert.equal(targetLabel(targetOf({ environment_simulated: false, environment_kind: "sregym" })), "Live target · SREGym");
  assert.equal(targetLabel(targetOf({ environment_simulated: false, environment_kind: null })), "Live target · kind not reported");
  assert.equal(targetLabel(targetOf({ environment_simulated: null, environment_kind: null })), "Target not recorded");
});

test("outcomes on the simulator and on each real cluster are counted apart, never added together", () => {
  const runs = [
    ...SEEDED,
    clusterRun("kind", { outcome: "resolved" }),
    clusterRun("kind", { outcome: "approval_rejected" }),
    clusterRun("kind", { outcome: "approval_rejected" }),
    clusterRun("sregym", { outcome: "resolved" }),
    clusterRun("sregym", { outcome: "safety_blocked" }),
    run({ provider_class: "local_llm", provider_kind: "llm", scenario_id: "bad-deployment" }),
  ];
  const groups = outcomesBySource(runs);
  assert.deepEqual(
    groups.map((g) => `${g.source}|${g.target?.key ?? "none"}`),
    ["scripted|simulated", "replay|simulated", "local_llm|simulated", "local_llm|live:kind", "local_llm|live:sregym", "free_hosted_llm|none"],
  );
  const [, , localSim, localKind, localSregym, hosted] = groups;
  assert.equal(localSim.total, 1);
  assert.equal(localKind.total, 3);
  assert.deepEqual(localKind.outcomes, [
    { outcome: "resolved", count: 1 },
    { outcome: "approval_rejected", count: 2 },
  ]);
  assert.equal(localSregym.total, 2);
  assert.equal(localSregym.resolved, 1);
  // The always-shown source without any run is one empty column with no target to name.
  assert.equal(hosted.total, 0);
  assert.equal(hosted.target, null);
  // No column holds runs of two targets.
  assert.equal(groups.reduce((n, g) => n + g.total, 0), runs.length);
  // Old runs that recorded no target stay apart from both.
  const old = outcomesBySource([run({ environment_simulated: null, environment_kind: null }), run({})]);
  assert.deepEqual(
    old.filter((g) => g.source === "scripted").map((g) => g.target?.key),
    ["simulated", "unknown"],
  );
});

test("a column's target note: none for the simulator, a real cluster said plainly, SREGym's grader kept apart", () => {
  assert.equal(targetNote(null), null);
  assert.equal(targetNote({ simulated: true, kind: "simulated" }), null);
  assert.equal(targetNote({ simulated: false, kind: "kind" }), "These runs acted on a real cluster, not a simulation.");
  assert.match(targetNote({ simulated: false, kind: "sregym" }) ?? "", /RECAP's own oracle; SREGym's grader gives the benchmark's verdict separately/);
  assert.match(targetNote({ simulated: null, kind: null }) ?? "", /did not record which environment/);
  // A live adapter this repository does not describe is live, but not called a cluster.
  assert.equal(targetNote({ simulated: false, kind: "custom.module:Adapter" }), "These runs acted on a live target, not a simulation.");
  assert.equal(targetNote({ simulated: false, kind: null }), "These runs acted on a live target, not a simulation.");
});

test("the local-LLM note covers a self-hosted model on this machine or the owner's VM", () => {
  assert.match(SOURCE_NOTES.local_llm, /self-hosted open-weight model \(Ollama\), on this machine or on the owner's own VM/);
  // The M5 runs used ollama-vm on the owner's VM: the note no longer says "this machine" only.
  assert.doesNotMatch(SOURCE_NOTES.local_llm, /^New inference by a model on this machine/);
});

test("replaying one recording again counts as one recording", () => {
  const again = replayOf("qwen3-8b-unfamiliar-parallel", "20261025-121828-ollama-6a5cfd");
  const [, replay] = outcomesBySource([run({ provider_class: "replay", replay: again }), run({ provider_class: "replay", replay: again })]);
  assert.equal(replay.total, 2);
  assert.equal(replay.recordings, 1);
});

test("unusual sources and outcomes appear only when present, in a fixed order", () => {
  // (All on the simulator: the target split is tested above.)
  const groups = outcomesBySource([
    run({ provider_class: null, provider_kind: null, outcome: "failed" }),
    run({ provider_class: "paid_llm", provider_kind: "llm", outcome: "error" }),
    run({ provider_class: "local_llm", provider_kind: "llm", outcome: "quota_deferred" }),
    run({ provider_class: "local_llm", provider_kind: "llm", outcome: "in_progress", terminal: false }),
    // An outcome a newer server might send: shown as sent, never dropped.
    run({ provider_class: "local_llm", provider_kind: "llm", outcome: "brand_new_outcome" as RunSummary["outcome"] }),
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
  assert.equal(livenessLook("running").label, "Running");
  assert.equal(livenessLook("queued").label, "Queued for a worker");
  assert.equal(livenessLook("interrupted").tone, "warn");
  assert.equal(livenessLook("cancelled").label, "Cancelled before it started");
  assert.equal(livenessLook("external").label, "Not running in this server");
  assert.equal(livenessLook("error").tone, "bad");
  assert.equal(livenessLook("unknown").label, "Status unknown");
});

test("at a glance: resolved, blocked, failed, active, and anything else apart; never a rate", () => {
  const outcomes = ["resolved", "resolved", "safety_blocked", "approval_rejected", "approval_expired", "failed", "error", "budget_exhausted", "cancelled", "quota_deferred", "something_new"];
  const list = [...outcomes.map((outcome) => run({ outcome: outcome as never })), run({ terminal: false, outcome: "in_progress" }), run({ terminal: false, outcome: "resolved" })];
  assert.deepEqual(glanceCounts(list), { resolved: 2, blocked: 3, failed: 3, active: 2, other: 3 });
  assert.deepEqual(glanceCounts([]), { resolved: 0, blocked: 0, failed: 0, active: 0, other: 0 });
  assert.deepEqual(
    GLANCE_GROUPS.map((g) => [g.key, g.label, g.tone]),
    [
      ["resolved", "Resolved", "ok"],
      ["blocked", "Blocked", "warn"],
      ["failed", "Failed", "bad"],
      ["active", "Active", "accent"],
      ["other", "Cancelled or deferred", "neutral"],
    ],
  );
  for (const group of GLANCE_GROUPS) assert.doesNotMatch(group.hint, /%|rate/);
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
    ...KUBERNETES_AUTHORIZATION.flatMap((step) => step.links),
  ]);
  for (const path of paths) assert.ok(existsSync(repo + path), `missing in the repository: ${path}`);
});

test("the flow map says what M5 built: no step calls Kind or SREGym planned, or RECAP simulator-only", () => {
  const text = JSON.stringify([ARCH_NODES, KUBERNETES_AUTHORIZATION]);
  for (const stale of [/planned for M5/i, /nothing in RECAP runs on a real cluster/i, /documentation only/i, /interface only/i, /EnvironmentNotIntegrated/]) {
    assert.doesNotMatch(text, stale);
  }
  // The M5 modules, manifests and ADRs are linked from the steps they belong to.
  const modules = (id: string) => archNode(id)!.modules;
  assert.ok(modules("environment").includes("recap/environments/kubernetes/"));
  assert.ok(modules("environment").includes("integrations/sregym/"));
  assert.ok(modules("environment").includes("configs/targets/"));
  assert.ok(modules("alert").includes("fixtures/kind-scenarios/") && modules("alert").includes("fixtures/sregym-scenarios/"));
  assert.ok(modules("write").includes("recap/environments/kubernetes/engine.py"));
  assert.ok(modules("write").includes("infra/kind/manifests/") && modules("write").includes("infra/sregym/recap-rbac.yaml"));
  assert.ok(modules("oracle").includes("recap/environments/kubernetes/probe.py"));
  for (const id of ["0029", "0030", "0031"]) assert.ok(ADRS.has(id), `ADR ${id}`);
  for (const id of ["alert", "write", "oracle", "environment"]) assert.ok(archNode(id)!.adrs.includes("0031"), `${id} links ADR 0031`);
});

test("the steps that differ by target explain all three, truthfully for each", () => {
  for (const id of ["alert", "approval", "write", "oracle", "environment"]) {
    const notes = archNode(id)!.onTarget ?? {};
    assert.deepEqual(Object.keys(notes).sort(), [...TARGET_KINDS].sort(), `${id} covers every target`);
  }
  for (const node of ARCH_NODES) for (const kind of Object.keys(node.onTarget ?? {})) assert.ok((TARGET_KINDS as readonly string[]).includes(kind));
  const on = (id: string, kind: "simulated" | "kind" | "sregym") => archNode(id)!.onTarget?.[kind] ?? "";
  // Who puts the problem in place: RECAP on the simulator and on Kind, SREGym's harness on SREGym.
  assert.match(on("alert", "kind"), /applies the scenario's fault steps as its own scenario identity/);
  assert.match(on("alert", "sregym"), /SREGym's own harness deploys the app and injects the problem/);
  assert.match(on("alert", "sregym"), /RECAP's reset changes nothing/);
  // The oracle: Kind has its own probe; SREGym has none, and its grader is separate.
  assert.match(on("oracle", "kind"), /a complete rollout, readiness, and its own HTTP probe/);
  assert.match(on("oracle", "sregym"), /No probe and no metrics/);
  assert.match(on("oracle", "sregym"), /containers that stayed up a minute with no exit in the last five/);
  assert.match(on("oracle", "sregym"), /SREGym's own grader gives the benchmark's verdict separately/);
  assert.doesNotMatch(on("oracle", "sregym"), /probe of/);
  // Approving on a real cluster takes the service's exact name; writes have their limits.
  assert.match(on("approval", "kind"), /exact name/);
  assert.match(on("write", "kind"), /admission policy/);
  assert.match(on("write", "sregym"), /no admission policy, so RBAC is the only limit/);
  assert.doesNotMatch(archNode("oracle")!.what.join(" "), /probes the environment/);
});

test("how a real Kubernetes action is authorized: every step in order, each with its code", () => {
  assert.deepEqual(
    KUBERNETES_AUTHORIZATION.map((step) => step.title),
    [
      "One exact proposal",
      "The verifier's checks",
      "A person types the service's name",
      "A signed, single-use capability",
      "The writer's limits",
      "Dry run, write, read-back, audit",
      "The independent oracle",
    ],
  );
  const links = new Set(KUBERNETES_AUTHORIZATION.flatMap((step) => step.links));
  for (const path of [
    "recap/environments/kubernetes/engine.py",
    "recap/safety/approvals.py",
    "recap/safety/capabilities.py",
    "infra/kind/manifests/",
    "infra/sregym/recap-rbac.yaml",
  ]) {
    assert.ok(links.has(path), `links ${path}`);
  }
  const text = KUBERNETES_AUTHORIZATION.map((step) => step.text).join(" ");
  assert.match(text, /types the target service's exact name, at the console or in this dashboard/);
  assert.match(text, /commits its nonce before touching the cluster/);
  assert.match(text, /patch only the app's named Deployments in one namespace/);
  assert.match(text, /On the Kind demo an admission policy also checks each patch's shape; SREGym's cluster has RBAC only\./);
  assert.match(text, /A server-side dry run first, then the write, a read-back of the Deployment, and an audit row/);
  for (const step of KUBERNETES_AUTHORIZATION) assert.ok(step.links.length >= 1, `${step.title} links its code`);
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
  assert.equal(facts("approval")["Approving needs the service's name typed"], "no (simulated target)");
  assert.equal(facts("write")["Actions it supports"], "rollback, restart, delete_namespace");
  assert.equal(facts("environment")["Adapter class"], "SimulatedEnvironment");
  assert.equal(facts("telemetry")["OpenTelemetry"], "off");
  // A server with no stored vectors says so instead of inventing an embedder.
  const bare = Object.fromEntries(nodeFacts(archNode("retrieval")!, { ...SYSTEM, embedder: null }));
  assert.equal(bare["Stored vectors"], "Not recorded");
});

test("node facts on a Kind and a SREGym server name the real cluster's adapter, policy and approval rule", () => {
  const kind = (id: string) => Object.fromEntries(nodeFacts(archNode(id)!, KIND_SYSTEM));
  assert.deepEqual(kind("environment"), {
    "Configured kind": "kind",
    "Adapter class": "KubernetesEnvironment",
    Namespace: "recap-demo",
    Simulated: "no",
    "Safety policy version": "policy-kind-v1",
  });
  assert.equal(kind("alert")["Environment"], "Kind cluster recap-m5 (kind kind)");
  assert.equal(kind("approval")["Approving needs the service's name typed"], "yes (not simulated)");
  assert.equal(kind("write")["Actions it supports"], "rollback, restart");
  assert.equal(kind("oracle")["Oracle"], "kind-health-oracle");
  const sregym = (id: string) => Object.fromEntries(nodeFacts(archNode(id)!, SREGYM_SYSTEM));
  assert.equal(sregym("environment")["Adapter class"], "SREGymEnvironment");
  assert.equal(sregym("environment")["Namespace"], "hotel-reservation");
  assert.equal(sregym("environment")["Safety policy version"], "policy-sregym-hotel-v2");
  assert.equal(sregym("oracle")["Oracle"], "sregym-health-oracle");
});
