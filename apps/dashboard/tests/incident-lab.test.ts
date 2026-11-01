/**
 * Tests for the Incident Lab's view helpers (src/view/lab.ts). Run with `npm test`. The fixtures
 * are shaped exactly like the API's JSON (apps/api/dto.py, src/dto.ts): GET /replays and
 * GET /providers as a seeded server returned them, and trace events from a recorded replay of
 * the golden qwen3:8b run against the simulated shop.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import type { FixtureView, OracleVerdict, ProviderView } from "../src/dto.ts";
import type { RunConfigInput, TraceEvent } from "../src/types.ts";
import {
  DEFAULT_PACE,
  FEED_PAGE,
  INSPECT_PAGES,
  MAX_STREAM_RESUMES,
  NOT_RECORDED,
  REPLAY_PACES,
  STAGE_HELP,
  approvalPhase,
  clampPace,
  feedStart,
  fixtureRouting,
  fixtureSource,
  fmtBytes,
  fmtOffset,
  initialProvider,
  integrityView,
  launchView,
  mergeBySeq,
  missingPrerequisites,
  oracleView,
  outcomeView,
  providerChoices,
  rationaleAuthor,
  readinessView,
  replayRunBody,
  resumeDelay,
  runBanner,
  runConfigBody,
  sortScenarios,
  stageTimes,
  statesOf,
  transportView,
  trustedRiskOf,
  withOverrides,
} from "../src/view/lab.ts";

// --- fixtures ----------------------------------------------------------------------------------

function fixture(over: Partial<FixtureView> = {}): FixtureView {
  return {
    id: "qwen3-8b-unfamiliar-parallel",
    title: "Unfamiliar incident, two specialists, owner-approved fix (M3 deliverable)",
    description: "qwen3:8b on the held-out m3-inventory-leak-behind-frontend incident.",
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

const LIMITS = {
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

function provider(over: Partial<ProviderView> & Pick<ProviderView, "name" | "kind" | "origin">): ProviderView {
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
    limits: LIMITS,
    limitations: [],
    ...over,
  };
}

/** GET /providers as a seeded server answered (keys only ever reported as present or not). */
const PROVIDERS: ProviderView[] = [
  provider({ name: "deterministic", kind: "scripted", origin: "scripted", default: true, selectable: true, selection_rule: "the server's default provider", detail: "scripted policy, not an LLM; $0" }),
  provider({ name: "ollama", kind: "llm", origin: "local_llm", model: "qwen3:8b", mode: "tools", selectable: true, selection_rule: "marked api_selectable in configs/providers.yaml by the owner" }),
  provider({
    name: "groq",
    kind: "llm",
    origin: "free_hosted_llm",
    model: "openai/gpt-oss-120b",
    status: "needs_setup",
    detail: "set GROQ_API_KEY in the environment",
    key_env: "GROQ_API_KEY",
    key_present: false,
    key_required: true,
    selection_rule: "chosen in server configuration only (api_selectable is off for it)",
  }),
  provider({ name: "replay", kind: "replay", origin: "replay", selectable: true, selection_rule: "only with replay_fixture, the id of a committed recording (GET /replays)" }),
  provider({ name: "anthropic", kind: "llm", origin: "paid_llm", status: "disabled", key_required: true, selection_rule: "a paid API: never selectable from the API (this project's budget is $0)" }),
];

let nextSeq = 1;
function ev(category: string, payload: Record<string, unknown>, ts: string, agent: string | null = null): TraceEvent {
  const seq = nextSeq++;
  return { event_id: `evt-${seq}`, run_id: "run-26c5fb3c51", seq, stage: "X", ts, agent, category, payload, duration_ms: null, usage: null, schema_version: "recap.v1" };
}

const REPLAY_PROVIDER = {
  name: "replay:ollama:qwen3:8b",
  kind: "replay" as const,
  origin: "replay" as const,
  model: "qwen3:8b",
  replay_of: { recording_id: "20261025-121828-ollama-6a5cfd", original_provider: "ollama", model: "qwen3:8b" },
};
const SIMULATED = { kind: "simulated", is_simulated: true };

function goldenTrace(): TraceEvent[] {
  nextSeq = 1;
  return [
    ev("state.transition", { from: null, to: "CREATED", provider: REPLAY_PROVIDER, environment: SIMULATED }, "2026-10-26T05:08:47.260809Z"),
    ev("state.transition", { from: "CREATED", to: "RETRIEVING" }, "2026-10-26T05:08:48.700000Z"),
    ev("state.transition", { from: "RETRIEVING", to: "INVESTIGATING" }, "2026-10-26T05:08:48.701000Z"),
    ev("state.transition", { from: "INVESTIGATING", to: "PROPOSING" }, "2026-10-26T05:09:10.750000Z"),
    ev(
      "mitigation.proposed",
      { proposal: { proposal_id: "prop-0d046a159f", action: "restart_service", target: "inventory" }, trusted_risk: "reversible_write" },
      "2026-10-26T05:09:14.138047Z",
    ),
    ev("state.transition", { from: "PROPOSING", to: "AWAITING_APPROVAL" }, "2026-10-26T05:09:14.147000Z"),
    ev(
      "approval.requested",
      { proposal_id: "prop-0d046a159f", digest: "99ea7f82a851", expires_at: "2026-10-26T05:24:14.148202+00:00", channel: "external (API/UI)" },
      "2026-10-26T05:09:14.148202Z",
    ),
  ];
}

// --- replay pace and request ---------------------------------------------------------------------

test("the default pace is watchable (0.05); instant is 0", () => {
  assert.equal(DEFAULT_PACE, 0.05);
  assert.deepEqual(
    REPLAY_PACES.map((p) => [p.key, p.value, p.label]),
    [
      ["watchable", 0.05, "Watchable (about half a minute)"],
      ["instant", 0, "Instant"],
    ],
  );
  for (const pace of REPLAY_PACES) assert.match(pace.hint, /never what is replayed/);
});

test("a pace stays inside RunConfig.replay_pace's 0..1", () => {
  assert.equal(clampPace(0.05), 0.05);
  assert.equal(clampPace(-1), 0);
  assert.equal(clampPace(2), 1);
  assert.equal(clampPace(Number.NaN), 0);
});

test("a replay asks exactly what api.startReplay asks, plus the pace, and always manual approval", () => {
  assert.deepEqual(replayRunBody(fixture(), 0.05), {
    scenario_id: "m3-inventory-leak-behind-frontend",
    config: { provider: "replay", replay_fixture: "qwen3-8b-unfamiliar-parallel", approval_mode: "manual", replay_pace: 0.05 },
  });
  const body = replayRunBody(fixture({ id: "qwen3-8b-bad-deployment", scenario: "bad-deployment" }), 0);
  assert.equal(body.config.replay_pace, 0);
  // Routing, memory and retrieval come from the recording on the server: never sent.
  for (const key of ["strategy", "use_memory", "retrieval", "use_rag", "tool_latency_ms"]) assert.ok(!(key in body.config), key);
});

// --- recordings ------------------------------------------------------------------------------------

test("integrity reads intact, altered or missing, in words, with the pinned hash", () => {
  const intact = integrityView("intact", "37830779420b");
  assert.equal(intact.label, "Intact");
  assert.equal(intact.tone, "ok");
  assert.match(intact.detail, /sha256 37830779420b/);
  assert.equal(integrityView("altered", "abc").tone, "bad");
  assert.match(integrityView("altered", "abc").detail, /never replays an altered recording/);
  assert.equal(integrityView("missing").label, "Missing");
  assert.equal(integrityView(undefined).label, NOT_RECORDED);
});

test("a recording that cannot be replayed keeps the server's reasons, or says it gave none", () => {
  assert.deepEqual(launchView(fixture()), { launchable: true, reasons: [] });
  const altered = fixture({
    launchable: false,
    not_launchable_because: ["its bytes do not match the SHA-256 the manifest pins (altered)", "  "],
  });
  assert.deepEqual(launchView(altered).reasons, ["its bytes do not match the SHA-256 the manifest pins (altered)"]);
  assert.deepEqual(launchView(fixture({ launchable: false, not_launchable_because: [] })).reasons, ["The server did not say why."]);
});

test("a replay's data source is a replay of the recorded model, never live inference", () => {
  const source = fixtureSource(fixture());
  assert.equal(source.origin, "replay");
  assert.equal(source.kind, "replay");
  assert.equal(source.model, "qwen3:8b");
  assert.deepEqual(source.replay_of, { original_provider: "ollama", model: "qwen3:8b", recording_id: "20261025-121828-ollama-6a5cfd" });
});

test("routing and size come from the manifest entry", () => {
  assert.equal(fixtureRouting(fixture()), "parallel routing · memory on · bm25 search");
  assert.equal(fixtureRouting(fixture({ strategy: "adaptive", use_memory: false })), "adaptive routing · memory off · bm25 search");
  assert.equal(fixtureRouting({ strategy: "", use_memory: undefined as unknown as boolean, retrieval: "" }), NOT_RECORDED);
  assert.equal(fmtBytes(225578), "225,578 bytes");
  assert.equal(fmtBytes(null), NOT_RECORDED);
});

// --- backends ----------------------------------------------------------------------------------------

test("only selectable backends are offered; replay is left to the replay panel; the rest say why not", () => {
  const choices = providerChoices(PROVIDERS);
  assert.deepEqual(
    choices.choosable.map((p) => p.name),
    ["deterministic", "ollama"],
  );
  assert.equal(choices.replay?.name, "replay");
  assert.deepEqual(
    choices.unavailable.map((u) => [u.provider.name, u.reason]),
    [
      ["groq", "chosen in server configuration only (api_selectable is off for it)"],
      ["anthropic", "a paid API: never selectable from the API (this project's budget is $0)"],
    ],
  );
  assert.equal(choices.defaultName, "deterministic");
  assert.equal(initialProvider(choices), "deterministic");
});

test("the default comes first; without a choosable default the first choosable backend is preselected", () => {
  const reordered = providerChoices([PROVIDERS[1], PROVIDERS[0]]);
  assert.deepEqual(reordered.choosable.map((p) => p.name), ["deterministic", "ollama"]);
  const noDefault = providerChoices([PROVIDERS[1], { ...PROVIDERS[2], default: true }]);
  assert.equal(noDefault.defaultName, "groq");
  assert.equal(initialProvider(noDefault), "ollama");
  assert.equal(initialProvider(providerChoices([])), null);
  assert.equal(providerChoices([{ ...PROVIDERS[2], selection_rule: "" }]).unavailable[0].reason, "The server did not say why.");
});

test("readiness is stated in words with its own icon", () => {
  assert.deepEqual([readinessView("ready").label, readinessView("ready").tone], ["Ready", "ok"]);
  assert.equal(readinessView("needs_setup", "set GROQ_API_KEY in the environment").detail, "set GROQ_API_KEY in the environment");
  assert.equal(readinessView("stopped").icon, "stop");
  assert.equal(readinessView("disabled").tone, "neutral");
  assert.equal(readinessView("unavailable").label, "Unavailable");
  assert.equal(readinessView("warming_up").label, "warming up");
  assert.equal(readinessView(null).label, NOT_RECORDED);
});

test("a scenario run sends provider only when it is not the server's default", () => {
  const config: RunConfigInput = { strategy: "adaptive", use_rag: true, use_memory: true, approval_mode: "manual", tool_latency_ms: 120 };
  assert.ok(!("provider" in runConfigBody(config, "deterministic", "deterministic")));
  assert.ok(!("provider" in runConfigBody(config, null, "deterministic")));
  assert.equal(runConfigBody(config, "ollama", "deterministic").provider, "ollama");
  // A stale replay field never leaks into a scenario run.
  assert.ok(!("replay_fixture" in runConfigBody({ ...config, replay_fixture: "x", provider: "replay" }, "deterministic", "deterministic")));
  assert.equal(config.provider, undefined); // the input is not changed
});

// --- scenarios -----------------------------------------------------------------------------------------

test("demo scenarios come first; overrides and prerequisites come from the scenario", () => {
  const base = { title: "", description: "", family: "", alert_service: "", tags: [], expected: {}, default_seed: null, budget: {}, requires_memory_of: [] };
  const sorted = sortScenarios([
    { ...base, id: "test-a", partition: "test", run_overrides: {} },
    { ...base, id: "demo-a", partition: "demo", run_overrides: {} },
    { ...base, id: "train-a", partition: "train", run_overrides: {} },
  ]);
  assert.deepEqual(sorted.map((s) => s.id), ["demo-a", "train-a", "test-a"]);
  const config: RunConfigInput = { strategy: "adaptive", use_rag: true, use_memory: true, approval_mode: "manual", tool_latency_ms: 120 };
  assert.equal(withOverrides(config, { run_overrides: { strategy: "parallel", planner: "prompt_injected" } }).strategy, "parallel");
  assert.equal(withOverrides(config, { run_overrides: { strategy: "sideways" } }).strategy, "adaptive");
  const episodes = [{ scenario_id: "bad-deployment", outcome: "success" as const, verified: true }];
  assert.deepEqual(missingPrerequisites({ requires_memory_of: ["bad-deployment"] }, episodes), []);
  assert.deepEqual(missingPrerequisites({ requires_memory_of: ["bad-deployment"] }, []), ["bad-deployment"]);
  assert.deepEqual(missingPrerequisites({ requires_memory_of: ["bad-deployment"] }, undefined), []);
});

// --- the run banner --------------------------------------------------------------------------------------

test("a replay on the simulator reads RECORDED REAL-LLM RUN · SIMULATED TARGET", () => {
  const banner = runBanner(REPLAY_PROVIDER, SIMULATED);
  assert.equal(banner.label, "RECORDED REAL-LLM RUN · SIMULATED TARGET");
  assert.equal(banner.tone, "warn");
  assert.match(banner.explanation, /qwen3:8b via ollama/);
  assert.match(banner.explanation, /no model is called now/);
  assert.match(banner.explanation, /simulated shop/);
});

test("scripted, live local and unknown sources are told apart, and a live target is red", () => {
  assert.equal(runBanner({ origin: "scripted", kind: "scripted" }, SIMULATED).label, "SCRIPTED BASELINE (NOT AN LLM) · SIMULATED TARGET");
  // A run recorded before origins existed: its kind alone.
  assert.equal(runBanner({ kind: "scripted" }, SIMULATED).decisions, "SCRIPTED BASELINE (NOT AN LLM)");
  const local = runBanner({ origin: "local_llm", kind: "llm", model: "qwen3:8b" }, SIMULATED);
  assert.equal(local.label, "LIVE LOCAL LLM · SIMULATED TARGET");
  assert.match(local.explanation, /qwen3:8b running on this machine/);
  assert.equal(runBanner(null, null).label, "SOURCE NOT RECORDED · TARGET NOT RECORDED");
  // A model's name never decides the label.
  assert.equal(runBanner({ kind: "llm", model: "replay-of-something" }, SIMULATED).decisions, "SOURCE NOT RECORDED");
  const live = runBanner({ origin: "local_llm", kind: "llm" }, { is_simulated: false, kind: "kind" });
  assert.equal(live.target, "LIVE TARGET · kind");
  assert.equal(live.tone, "bad");
});

// --- stages and approval ---------------------------------------------------------------------------------

test("states and first-entry times come from state.transition events", () => {
  const events = goldenTrace();
  assert.deepEqual(statesOf(events), ["CREATED", "RETRIEVING", "INVESTIGATING", "PROPOSING", "AWAITING_APPROVAL"]);
  const times = stageTimes(events);
  assert.equal(times.CREATED, 0);
  assert.equal(Math.round(times.INVESTIGATING), 1441); // 05:08:48.701 - 05:08:47.260 (microseconds trimmed)
  assert.equal(fmtOffset(times.INVESTIGATING), "+1.4 s");
  assert.equal(fmtOffset(26_887), "+27 s");
  assert.equal(fmtOffset(61_000), "+1 min 1 s");
  assert.equal(fmtOffset(undefined), "");
  assert.deepEqual(stageTimes([]), {});
  for (const state of ["CREATED", "INVESTIGATING", "AWAITING_APPROVAL", "RESOLVED", "BLOCKED"]) assert.ok(STAGE_HELP[state], state);
});

test("a manual request waits for a person; the trusted risk comes from the proposal event", () => {
  const events = goldenTrace();
  const phase = approvalPhase(events);
  assert.equal(phase.kind, "waiting");
  assert.ok(phase.kind === "waiting" && !phase.auto && phase.proposalId === "prop-0d046a159f");
  assert.equal(trustedRiskOf(events, "prop-0d046a159f"), "reversible_write");
  assert.equal(trustedRiskOf(events, "prop-other"), null);
  assert.deepEqual(approvalPhase(events.slice(0, 3)), { kind: "none" });
});

test("approved, rejected and expired decisions are read from approval.decided", () => {
  for (const [decision, actor] of [
    ["approved", "operator:local-operator"],
    ["rejected", "operator:local-operator"],
    ["expired", null],
  ] as const) {
    const events = goldenTrace();
    events.push(ev("approval.decided", { decision, ...(actor ? { actor, approval_id: "apr-a15df3cc84" } : {}), proposal_id: "prop-0d046a159f" }, "2026-10-26T05:09:41Z"));
    const phase = approvalPhase(events);
    assert.equal(phase.kind, "decided");
    assert.ok(phase.kind === "decided" && phase.decision === decision && phase.actor === actor);
  }
});

test("the simulation auto-approver is never shown as a person, and a newer request waits again", () => {
  const events = goldenTrace();
  events[6] = { ...events[6], payload: { ...events[6].payload, channel: "auto-approver (simulation only)" } };
  const phase = approvalPhase(events);
  assert.ok(phase.kind === "waiting" && phase.auto);
  events.push(ev("approval.decided", { decision: "rejected", proposal_id: "prop-0d046a159f" }, "2026-10-26T05:09:41Z"));
  events.push(ev("approval.requested", { proposal_id: "prop-second", channel: "external (API/UI)" }, "2026-10-26T05:09:50Z"));
  const again = approvalPhase(events);
  assert.ok(again.kind === "waiting" && again.proposalId === "prop-second" && !again.auto);
});

test("the rationale's author comes from the server's TextAuthor, else from the run's provider", () => {
  assert.deepEqual(rationaleAuthor("replayed_model", REPLAY_PROVIDER), { who: "the recorded model (qwen3:8b), replayed", trusted: false });
  assert.deepEqual(rationaleAuthor("model", { origin: "local_llm", model: "qwen3:8b" }), { who: "the model (qwen3:8b)", trusted: false });
  assert.deepEqual(rationaleAuthor("scripted_policy", null), { who: "the scripted policy", trusted: false });
  assert.deepEqual(rationaleAuthor("recap", null), { who: "RECAP", trusted: true });
  assert.equal(rationaleAuthor(null, REPLAY_PROVIDER).who, "the recorded model (qwen3:8b), replayed");
  assert.equal(rationaleAuthor(undefined, { kind: "scripted" }).who, "the scripted policy");
  assert.deepEqual(rationaleAuthor("unknown", null), { who: "the proposer", trusted: false });
});

// --- outcome and oracle -------------------------------------------------------------------------------------

test("every outcome the API defines reads in plain words; an unknown one reads Not recorded", () => {
  const outcomes = ["in_progress", "resolved", "failed", "error", "cancelled", "approval_rejected", "approval_expired", "safety_blocked", "budget_exhausted", "quota_deferred"];
  for (const outcome of outcomes) {
    const view = outcomeView(outcome);
    assert.notEqual(view.label, NOT_RECORDED, outcome);
    assert.ok(view.sentence.endsWith("."), outcome);
  }
  assert.equal(outcomeView("resolved").tone, "ok");
  assert.match(outcomeView("resolved").sentence, /independent oracle verified/);
  assert.equal(outcomeView("approval_rejected").label, "Blocked: fix rejected");
  assert.equal(outcomeView("something-new").label, NOT_RECORDED);
  assert.equal(outcomeView(null).label, NOT_RECORDED);
});

test("the oracle's verdict, or why it was not consulted", () => {
  const verified: OracleVerdict = {
    oracle: "simulated-health-oracle",
    verified: true,
    healthy_before: false,
    healthy_after: true,
    checked_at: "2026-03-18T19:45:00Z",
    services_checked: ["cart", "checkout", "frontend", "inventory", "payment"],
  };
  const view = oracleView(verified);
  assert.deepEqual([view.label, view.tone, view.before, view.after, view.detail], ["Recovery verified", "ok", "unhealthy", "healthy", "simulated-health-oracle"]);
  assert.equal(view.services.length, 5);
  const failed = oracleView({ ...verified, verified: false, healthy_after: false });
  assert.deepEqual([failed.label, failed.tone, failed.after], ["Not recovered", "bad", "unhealthy"]);
  const none = oracleView(null);
  assert.equal(none.label, "Not consulted");
  assert.match(none.detail, /No fix reached the oracle/);
});

test("the run links cover the five run pages", () => {
  assert.deepEqual(
    INSPECT_PAGES.map((p) => p.page),
    ["timeline", "evidence", "knowledge", "memory", "safety"],
  );
});

// --- feed: merging, resuming, bounding ------------------------------------------------------------------------

test("events merge by seq: in order, without duplicates, and unchanged when nothing is new", () => {
  const a = { seq: 1 };
  const b = { seq: 2 };
  const c = { seq: 3 };
  const list = [a, b];
  assert.deepEqual(mergeBySeq(list, [c]), [a, b, c]);
  assert.equal(mergeBySeq(list, []), list);
  assert.equal(mergeBySeq(list, [b]), list); // a resumed stream re-sending an event changes nothing
  assert.deepEqual(mergeBySeq([a, c], [b, c]).map((e) => e.seq), [1, 2, 3]);
  assert.deepEqual(mergeBySeq([], [c, a, b, a]).map((e) => e.seq), [1, 2, 3]);
});

test("a broken stream is reopened three times (1 s, 2 s, 4 s), then the feed polls", () => {
  assert.equal(MAX_STREAM_RESUMES, 3);
  assert.deepEqual([0, 1, 2, 3].map(resumeDelay), [1000, 2000, 4000, null]);
  assert.equal(resumeDelay(-1), null);
  assert.equal(resumeDelay(1.5), null);
});

test("the transport says how events arrive, and after which event a stream resumed", () => {
  assert.equal(transportView("sse", 0, 12).label, "Live stream (SSE)");
  assert.equal(transportView("sse", 0, 12).detail, "");
  assert.equal(transportView("sse", 2, 118).detail, "reconnected 2× · resumed after event #118");
  assert.equal(transportView("reconnecting", 1, 118).detail, "will resume after event #118");
  assert.equal(transportView("polling", 0, 0).label, "Polling /events");
  assert.match(transportView("recorded", 0, 0).detail, /another process/);
  assert.equal(transportView("idle", 0, 0).label, "Connecting");
  // Once the run has finished, the stream is complete (or stopped on an error), never "live".
  assert.deepEqual([transportView("sse", 0, 128, true, 128).label, transportView("sse", 0, 128, true, 128).detail], ["Stream complete", "128 events received"]);
  assert.equal(transportView("polling", 2, 128, true, 128).detail, "128 events received · reconnected 2×");
  assert.equal(transportView("sse", 0, 40, true, 40, true).label, "Stream stopped");
  assert.equal(transportView("recorded", 0, 40, true, 40).label, "Recorded events");
});

test("the feed renders at most a page of lines while following, and does not move while paused", () => {
  assert.equal(FEED_PAGE, 200);
  assert.equal(feedStart(1200, 200, null), 1000);
  assert.equal(feedStart(150, 200, null), 0);
  assert.equal(feedStart(1250, 200, 1000), 1000); // paused at 1000: new lines arrive below
  assert.equal(feedStart(1250, 400, 800), 800);
  assert.equal(feedStart(10, 200, 50), 10); // a filter left fewer lines than the paused start
  assert.equal(feedStart(0, 200, null), 0);
});

test("hostile text passes through the helpers unchanged (components escape it)", () => {
  const hostile = `<img src=x onerror="alert(1)"> javascript:alert(1)`;
  assert.deepEqual(launchView(fixture({ launchable: false, not_launchable_because: [hostile] })).reasons, [hostile]);
  const choices = providerChoices([{ ...PROVIDERS[2], selection_rule: hostile }]);
  assert.equal(choices.unavailable[0].reason, hostile);
});
