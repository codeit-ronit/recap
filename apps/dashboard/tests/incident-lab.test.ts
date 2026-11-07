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
  approvalTarget,
  approveGate,
  attemptLabel,
  clampPace,
  decisionBody,
  feedStart,
  fixtureRouting,
  fixtureSource,
  fmtBytes,
  fmtOffset,
  healthWords,
  initialProvider,
  integrityView,
  labIntro,
  launchView,
  liveTargetWords,
  mergeBySeq,
  missingPrerequisites,
  namesTarget,
  oracleView,
  outcomeView,
  providerChoices,
  rationaleAuthor,
  readinessView,
  recoverySignals,
  replayBlockedNote,
  replayRunBody,
  resumeDelay,
  runBanner,
  runConfigBody,
  scenarioIntro,
  settleWords,
  sortScenarios,
  stageHelp,
  stageTimes,
  statesOf,
  summaryTarget,
  tamper,
  targetFacts,
  targetPlace,
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
  assert.match(local.explanation, /qwen3:8b, self-hosted \(this machine or the owner's own VM\)/);
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

// --- a live target (M5): where actions land, the typed name, the live words ------------------------------

/** The Kind target as GET /system and the run's first event report it (configs/targets/kind-demo). */
const KIND = {
  kind: "kind",
  name: "Kind cluster recap-m5",
  namespace: "recap-demo",
  is_simulated: false,
  description: "Local Kind cluster recap-m5 (context kind-recap-m5), not production.",
  stabilization_timeout_seconds: 180,
};
const SHOP = { kind: "simulated", name: "RECAP simulated shop", namespace: "shop", is_simulated: true, stabilization_timeout_seconds: 0 };

test("the target is read from what the server reported: simulated, live or not reported, never guessed", () => {
  assert.deepEqual(targetFacts(KIND), {
    state: "live",
    kind: "kind",
    name: "Kind cluster recap-m5",
    namespace: "recap-demo",
    description: "Local Kind cluster recap-m5 (context kind-recap-m5), not production.",
    settleSeconds: 180,
  });
  assert.equal(targetFacts(SHOP).state, "simulated");
  assert.equal(targetFacts(SHOP).settleSeconds, null, "the simulator gets no settle window");
  assert.equal(targetFacts(null).state, "unknown");
  assert.equal(targetFacts({ kind: "kind", name: "x" }).state, "unknown", "a kind alone does not say live or simulated");
  assert.equal(targetPlace(targetFacts(KIND)), "Kind cluster recap-m5, namespace recap-demo");
  assert.equal(targetPlace(targetFacts({ is_simulated: false, kind: "kind" })), "");
  assert.deepEqual(liveTargetWords(targetFacts(KIND)), { place: "Kind cluster recap-m5, namespace recap-demo", real: "a real cluster" });
  assert.deepEqual(liveTargetWords(targetFacts({ is_simulated: false, kind: "other" })), { place: "the live other", real: "a real system" });
  assert.deepEqual(summaryTarget({ environment_kind: "kind", environment_simulated: false }), { is_simulated: false, kind: "kind" });
  assert.equal(summaryTarget({ environment_kind: null, environment_simulated: null }), null);
});

test("an approval acts on the live target if either the run or the server says live; else on what is known", () => {
  assert.equal(approvalTarget(KIND, SHOP), KIND);
  assert.equal(approvalTarget(SHOP, KIND), KIND, "a typed name is never skipped because one source said simulated");
  assert.equal(approvalTarget(SHOP, null), SHOP);
  assert.equal(approvalTarget(null, SHOP), SHOP);
  assert.equal(approvalTarget({ kind: "kind" }, null), null, "a source that does not say simulated or not counts for nothing");
  assert.equal(approvalTarget(null, undefined), null);
});

test("the typed name must be the proposal's target exactly, as the server compares it (spaces around it aside)", () => {
  assert.ok(namesTarget("backend", "backend"));
  assert.ok(namesTarget("  backend\t", "backend"));
  for (const typed of ["Backend", "BACKEND", "back end", "backend2", "y", "yes", "", "   "]) assert.ok(!namesTarget(typed, "backend"), typed);
  assert.ok(!namesTarget("", ""), "an empty target is never named");
  assert.ok(!namesTarget(null, "backend"));
  assert.ok(!namesTarget("backend", null));
});

test("Approve waits for the name on a live target, for GET /system while it is asked, and never when nothing can be decided", () => {
  const open = { canDecide: true, checking: false, needsName: true, service: "backend" };
  assert.deepEqual(approveGate({ ...open, typed: "" }), { blocked: true, why: "Type backend above to enable Approve" });
  assert.deepEqual(approveGate({ ...open, typed: "Backend" }), { blocked: true, why: "Type backend above to enable Approve" });
  assert.deepEqual(approveGate({ ...open, typed: "backend" }), { blocked: false, why: null });
  assert.deepEqual(approveGate({ ...open, needsName: false, typed: "" }), { blocked: false, why: null }, "the simulated shop: one click");
  assert.deepEqual(approveGate({ ...open, checking: true, typed: "backend" }), { blocked: true, why: "Checking which environment this approval acts on (GET /system)…" });
  assert.deepEqual(approveGate({ ...open, canDecide: false, typed: "backend" }), { blocked: true, why: null });
  assert.deepEqual(approveGate({ ...open, service: "", typed: "" }), { blocked: true, why: "Type the target service's name above to enable Approve" });
});

test("the decision body: the exact digest, the typed name with an approval only, and the binding test's other digest", () => {
  const digest = "917e98b72d1e4684f6aa77928897a945f90517f991a244aecdb3ae9f9e9c5239";
  assert.deepEqual(decisionBody(digest, { decision: "approved", confirmTarget: " backend " }), { decision: "approved", proposal_digest: digest, confirm_target: "backend" });
  assert.deepEqual(decisionBody(digest, { decision: "approved" }), { decision: "approved", proposal_digest: digest });
  assert.deepEqual(decisionBody(digest, { decision: "approved", confirmTarget: "   " }), { decision: "approved", proposal_digest: digest });
  assert.deepEqual(decisionBody(digest, { decision: "rejected", confirmTarget: "backend" }), { decision: "rejected", proposal_digest: digest }, "a rejection never sends the name");
  const tampered = decisionBody(digest, { decision: "approved", tampered: true });
  assert.notEqual(tampered.proposal_digest, digest);
  assert.equal(tampered.proposal_digest, tamper(digest));
  assert.equal(tamper("ab0"), "ab1");
  assert.equal(tamper(""), "0");
  assert.deepEqual(
    [attemptLabel({ decision: "approved" }), attemptLabel({ decision: "rejected" }), attemptLabel({ decision: "approved", tampered: true })],
    ["approve", "reject", "approve with a tampered digest"],
  );
});

test("a decision's note (how it came, RECAP's words) is read from approval.decided", () => {
  const events = goldenTrace();
  const note = "interactive terminal; typed the target service's name; answered after 167.8 s; login name kept in the local decision log";
  events.push(ev("approval.decided", { decision: "approved", actor: "console-user", approval_id: "apr-84ec57cc5a", proposal_id: "prop-0d046a159f", note }, "2026-11-03T13:13:08Z"));
  const phase = approvalPhase(events);
  assert.ok(phase.kind === "decided" && phase.note === note && phase.actor === "console-user");
  events.push(ev("approval.decided", { decision: "rejected", proposal_id: "prop-0d046a159f", note: "" }, "2026-11-03T13:14:00Z"));
  const plain = approvalPhase(events);
  assert.ok(plain.kind === "decided" && plain.note === null);
});

test("stage captions name the run's own target: the simulated shop, the live cluster, or neither", () => {
  assert.match(stageHelp("INVESTIGATING", SHOP), /read-only tools on the simulated shop/);
  assert.match(stageHelp("MITIGATING", SHOP), /to the simulated shop, once/);
  assert.match(stageHelp("VERIFYING", SHOP), /whether the shop recovered/);
  const live = ["INVESTIGATING", "AWAITING_APPROVAL", "MITIGATING", "VERIFYING"].map((s) => stageHelp(s, KIND));
  for (const caption of live) assert.doesNotMatch(caption, /simulated|shop/, caption);
  assert.match(live[0], /read-only tools on Kind cluster recap-m5, namespace recap-demo, a real cluster/);
  assert.match(live[1], /approving takes the target service's name, typed/);
  assert.match(live[2], /to Kind cluster recap-m5, namespace recap-demo, once: this changes a real cluster\./);
  assert.match(live[3], /probes Kind cluster recap-m5, namespace recap-demo until its services are healthy or the target's settle window \(up to 180 s\) ends/);
  assert.equal(stageHelp("INVESTIGATING", null), STAGE_HELP.INVESTIGATING);
  assert.doesNotMatch(STAGE_HELP.INVESTIGATING + STAGE_HELP.MITIGATING + STAGE_HELP.VERIFYING, /simulated|shop/, "the target-neutral words claim no target");
  assert.equal(stageHelp("RESOLVED", KIND), STAGE_HELP.RESOLVED);
  assert.equal(stageHelp(null, KIND), "");
});

test("the resolved sentence names where the recovery happened; other outcomes read the same on any target", () => {
  assert.equal(outcomeView("resolved", SHOP).sentence, "The independent oracle verified that the simulated shop recovered.");
  assert.equal(outcomeView("resolved", KIND).sentence, "The independent oracle verified that Kind cluster recap-m5, namespace recap-demo recovered: a real cluster, not a simulation.");
  assert.equal(outcomeView("resolved").sentence, "The independent oracle verified the recovery.");
  assert.equal(outcomeView("approval_rejected", KIND).sentence, outcomeView("approval_rejected").sentence);
});

test("the Lab's header, scenario card and replay note follow the server's target", () => {
  assert.match(labIntro(SHOP), /acts only on the simulated shop\.$/);
  const live = labIntro(KIND);
  assert.match(live, /^Start an incident on this server's live target/);
  assert.match(live, /The target is Kind cluster recap-m5, namespace recap-demo \(kind\): a real cluster, not a simulation/);
  assert.doesNotMatch(live, /simulated shop|Replay a recorded/);
  assert.match(labIntro(null), /the target shows here once GET \/system reports it\.$/);
  assert.match(scenarioIntro(SHOP), /^Inject a scenario's fault into the simulated shop/);
  assert.match(scenarioIntro(KIND), /^This target's scenarios \(GET \/scenarios\), on Kind cluster recap-m5, namespace recap-demo: a real cluster\./);
  assert.doesNotMatch(scenarioIntro(null), /simulated/);
  assert.match(replayBlockedNote(KIND) ?? "", /RECAP refuses a replay against it before it starts, because a recorded model's fix must never reach a real system/);
  assert.equal(replayBlockedNote(SHOP), null);
  assert.equal(replayBlockedNote(null), null);
});

test("a run on a live (or not reported) target always asks a person and sends no simulated latency", () => {
  const config: RunConfigInput = { strategy: "adaptive", use_rag: true, use_memory: true, approval_mode: "auto", tool_latency_ms: 120 };
  const live = runConfigBody(config, "ollama-vm", "deterministic", targetFacts(KIND));
  assert.equal(live.approval_mode, "manual");
  assert.equal(live.tool_latency_ms, null);
  assert.equal(live.provider, "ollama-vm");
  assert.equal(runConfigBody(config, null, null, targetFacts(null)).approval_mode, "manual");
  const simulated = runConfigBody(config, null, null, targetFacts(SHOP));
  assert.deepEqual([simulated.approval_mode, simulated.tool_latency_ms], ["auto", 120], "the simulated shop: as chosen");
  assert.deepEqual([runConfigBody(config, null, null).approval_mode, config.approval_mode], ["auto", "auto"], "no target given: unchanged, input untouched");
});

test("how long the oracle probed: a live target's settle window, the simulator's single probe, nothing invented", () => {
  const live = { oracle: "kind-health-oracle", verified: true, simulated: false, probes: 2, waited_seconds: 10.859 };
  assert.equal(settleWords(live, targetFacts(KIND)), "live target · 2 probes over 10.9 s (settle window 180 s)");
  assert.equal(settleWords({ simulated: false, probes: 9, waited_seconds: 66.931 }, targetFacts({ ...KIND, stabilization_timeout_seconds: 240 })), "live target · 9 probes over 66.9 s (settle window 240 s)");
  assert.equal(settleWords({ simulated: false, probes: 1, waited_seconds: 0.4 }), "live target · 1 probe");
  assert.equal(settleWords({ simulated: true, probes: 1, waited_seconds: 0 }, targetFacts(SHOP)), "simulated target · 1 probe");
  assert.equal(settleWords({ simulated: true }, targetFacts(SHOP)), "", "a run before M5 recorded no probes");
  assert.equal(settleWords(null), "");
});

test("the oracle's observations: one row per service, changed ones first, values as recorded", () => {
  const h = (status: string, ready: number, replicas: number, rate: number | null) => ({ service: "x", status, ready_replicas: ready, replicas, error_rate: rate });
  const rows = recoverySignals({
    before: { frontend: h("Running", 2, 2, 0.3), backend: h("Running", 2, 2, 0.2523), cache: h("Running", 1, 1, null) },
    after: { frontend: h("Running", 2, 2, 0), backend: h("Running", 2, 2, 0), cache: h("Running", 1, 1, null) },
  });
  assert.deepEqual(rows.map((r) => [r.service, r.changed]), [["frontend", true], ["backend", true], ["cache", false]]);
  assert.equal(healthWords(rows[1].before), "Running · 2/2 ready · errors 25.2%");
  assert.equal(healthWords(rows[0].after), "Running · 2/2 ready · errors 0%");
  assert.equal(healthWords(rows[2].after), "Running · 1/1 ready");
  assert.equal(healthWords(null), NOT_RECORDED);
  // A service only one side saw, and an empty or malformed record.
  const partial = recoverySignals({ before: {}, after: { geo: h("Running", 1, 1, null) } });
  assert.deepEqual([partial[0].before, partial[0].changed], [null, true]);
  assert.deepEqual(recoverySignals(null), []);
  assert.deepEqual(recoverySignals({ before: "x", after: [] }), []);
  assert.equal(healthWords({ status: NOT_RECORDED, ready: null, replicas: null, errorRate: null }), NOT_RECORDED);
});

test("a live run's banner names its cluster; the simulated one keeps its words", () => {
  const live = runBanner({ origin: "local_llm", kind: "llm", model: "qwen3:8b" }, KIND);
  assert.equal(live.label, "LIVE LOCAL LLM · LIVE TARGET · kind");
  assert.match(live.explanation, /It acts on Kind cluster recap-m5, namespace recap-demo: a real cluster, not a simulation, and an approved action changes it\./);
  assert.doesNotMatch(live.explanation, /simulated/);
  assert.match(runBanner({ origin: "scripted" }, SHOP).explanation, /Every action changes only RECAP's simulated shop\./);
});
