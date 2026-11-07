/**
 * Tests for the Guided Demo's view helpers (src/view/demo.ts) and the plain-English rule
 * explanations (src/view/explain.ts). Run with `npm test`.
 *
 * The traces in tests/fixtures are two real replays of the golden qwen3:8b recording against
 * the simulated shop, started with manual approval through the API (as the demo starts them)
 * and decided by the operator: one approved (RESOLVED), one rejected (BLOCKED). They are trimmed
 * (model output text, context-pack items and most retrieval results dropped), not edited.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import type { FixtureView } from "../src/dto.ts";
import type { TraceEvent } from "../src/types.ts";
import {
  DEMO_PACES,
  DEMO_PROVENANCE,
  DEMO_STEPS,
  activeActors,
  actionWords,
  approvalStory,
  demoNotices,
  demoPageHref,
  demoProgress,
  goldenFixture,
  paceFor,
  provenanceWords,
  recentActivity,
  recordedOn,
  stepOf,
  toolWords,
} from "../src/view/demo.ts";
import { explainRule, isExplained, ruleTitle } from "../src/view/explain.ts";

function trace(name: "approved" | "rejected"): TraceEvent[] {
  return JSON.parse(readFileSync(new URL(`./fixtures/golden-replay-${name}.json`, import.meta.url), "utf8")) as TraceEvent[];
}

const APPROVED = trace("approved");
const REJECTED = trace("rejected");

/** The trace up to and including the first event that matches. */
function until(events: TraceEvent[], match: (e: TraceEvent) => boolean): TraceEvent[] {
  const at = events.findIndex(match);
  assert.ok(at >= 0, "the fixture has the event");
  return events.slice(0, at + 1);
}

function fixture(overrides: Partial<FixtureView> = {}): FixtureView {
  return {
    id: "qwen3-8b-unfamiliar-parallel",
    title: "Unfamiliar incident, two specialists, owner-approved fix (M3 deliverable)",
    description: "",
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
    labels: [],
    ...overrides,
  } as FixtureView;
}

const texts = (facts: { text: string }[]) => facts.map((f) => f.text);

// --- the label and the steps -------------------------------------------------------------------------

test("the demo's provenance label is the specification's, word for word", () => {
  assert.equal(DEMO_PROVENANCE, "Recorded real-model execution — replayed locally");
});

test("ten steps, in the order a run really goes through them", () => {
  assert.deepEqual(
    DEMO_STEPS.map((s) => s.id),
    ["incident", "context", "strategy", "investigate", "verify", "propose", "approve", "act", "recover", "outcome"],
  );
  for (const step of DEMO_STEPS) {
    assert.ok(step.title && step.about && step.talk, step.id);
    assert.doesNotMatch(`${step.title} ${step.about}`, /\b[RV]P?\d+-/, `${step.id} says it in words, not codes`);
  }
});

test("every event of a real replay belongs to a step, and the steps follow the lifecycle", () => {
  const order = APPROVED.map((e) => stepOf(e));
  assert.ok(order.every((s) => s !== null));
  // Only an investigation's second round goes back (to the strategy step); nothing else ever does.
  const index = (id: string | null) => DEMO_STEPS.findIndex((s) => s.id === id);
  for (let i = 1; i < order.length; i += 1) {
    if (index(order[i]) < index(order[i - 1])) {
      assert.equal(order[i], "strategy", `event ${APPROVED[i].seq}`);
      assert.equal(order[i - 1], "verify");
    }
  }
});

// --- an approved replay, end to end -------------------------------------------------------------------

test("an approved replay: every step done, in order, with times from the trace", () => {
  const progress = demoProgress(APPROVED);
  assert.equal(progress.finished, true);
  assert.equal(progress.finalState, "RESOLVED");
  assert.equal(progress.round, 2);
  assert.deepEqual(
    progress.steps.map((s) => s.status),
    Array(10).fill("done"),
  );
  assert.deepEqual(
    progress.steps.map((s) => s.tone),
    Array(10).fill("ok"),
  );
  assert.equal(progress.current?.id, "outcome");
  const times = progress.steps.map((s) => s.at);
  assert.equal(times[0], 0);
  for (let i = 1; i < times.length; i += 1) assert.ok((times[i] ?? -1) >= (times[i - 1] ?? 0), `step ${i} starts after step ${i - 1}`);
  assert.equal(
    progress.steps.reduce((n, s) => n + s.events, 0),
    APPROVED.length,
  );
});

test("an approved replay: what each step found, in plain words, with codes kept aside", () => {
  const steps = Object.fromEntries(demoProgress(APPROVED).steps.map((s) => [s.id, s]));
  assert.deepEqual(texts(steps.incident.facts).slice(0, 2), [
    "Affected: frontend, cart.",
    "frontend: product pages take more than 4 seconds to render",
  ]);
  assert.match(texts(steps.incident.facts).at(-1) ?? "", /^Budget: 30 steps, 30 tool calls, 200,000 model tokens\.$/);
  assert.match(texts(steps.context.facts)[0], /^Runbooks: 3 relevant sections found \(bm25 search\); the best match is “Services” in shop-architecture\.md\.$/);
  assert.equal(texts(steps.context.facts)[1], "Experience memory: no similar past incident, so this incident is new to RECAP.");

  const strategy = steps.strategy.facts;
  assert.equal(strategy.length, 2);
  assert.match(strategy[0].text, /^Round 1: 2 specialists in parallel \(telemetry investigator and infrastructure investigator\), because parallel specialists forced by run configuration\.$/);
  assert.deepEqual(strategy[0].codes, ["RT0-forced"]);
  assert.match(strategy[1].text, /^Round 2: /);

  const investigate = texts(steps.investigate.facts);
  assert.match(investigate[0], /^2 agents made \d+ model requests and \d+ read-only tool calls, collecting \d+ evidence items\.$/);
  // The agents' own summaries are the model's words, marked with their author.
  const summaries = steps.investigate.facts.filter((f) => f.model);
  assert.deepEqual(
    summaries.map((f) => f.model),
    ["Telemetry investigator", "Infrastructure investigator"],
  );

  const verify = steps.verify.facts;
  assert.match(verify[0].text, /^Round 1: not enough evidence yet\. No actionable root cause is supported yet/);
  assert.equal(verify[0].tone, "warn");
  assert.match(verify.at(-1)?.text ?? "", /^Round 2: diagnosis verified\. Best supported root cause: resource pressure on inventory/);
  assert.equal(verify.at(-1)?.tone, "ok");

  const propose = steps.propose.facts;
  assert.equal(propose[0].text, "Proposed: restart the inventory service (restart_service on inventory).");
  assert.equal(propose[1].model, "the proposing model");
  const gate = propose.find((f) => f.text.startsWith("Evidence gate"));
  assert.equal(gate?.tone, "ok");
  assert.deepEqual(gate?.codes, ["VP1-evidence-exists", "VP2-target-matches-diagnosis", "VP3-action-named", "VP4-restore-point"]);
  const policy = propose.find((f) => f.text.startsWith("Safety policy"));
  assert.equal(policy?.text, "Safety policy: a person must approve this exact action.");
  assert.deepEqual(policy?.codes, ["R11-reversible-approval"]);

  assert.match(texts(steps.approve.facts)[0], /^Approved by operator:local-operator, \d+\.\d s after the request\.$/);
  const act = steps.act.facts;
  assert.equal(act[0].text, "Safety gateway: the approval matches this exact proposal, unused and unexpired.");
  assert.deepEqual(act[0].codes, ["R12-approval-binding"]);
  assert.equal(act[1].text, "restart_service ran once on inventory in the simulated shop: inventory: 2 pods restarted.");
  assert.equal(texts(steps.recover.facts)[0], "Recovery verified (simulated-health-oracle): the simulated shop was unhealthy before the fix and healthy after it.");
  assert.deepEqual(texts(steps.outcome.facts), [
    "RESOLVED: oracle verified recovery (simulated environment).",
    "Used 13 agent steps, 17 tool calls and 54,979 model tokens (as recorded).",
    "Not saved to experience memory: a replay reproduces a recorded run; it is not new experience.",
  ]);
});

// --- a rejected replay ---------------------------------------------------------------------------------

test("a rejected replay: the fix and the recovery check are skipped, and say why", () => {
  const progress = demoProgress(REJECTED);
  const steps = Object.fromEntries(progress.steps.map((s) => [s.id, s]));
  assert.equal(progress.finalState, "BLOCKED");
  assert.equal(steps.approve.status, "done");
  assert.equal(steps.approve.tone, "warn");
  assert.match(texts(steps.approve.facts)[0], /^Rejected by operator:local-operator, .* The fix will not run\.$/);
  for (const id of ["act", "recover"]) {
    assert.equal(steps[id].status, "skipped", id);
    assert.equal(steps[id].skippedBecause, "Not run: the fix was rejected, so nothing changed.");
    assert.deepEqual(steps[id].facts, []);
  }
  assert.equal(steps.outcome.status, "done");
  assert.equal(steps.outcome.tone, "warn");
  assert.equal(texts(steps.outcome.facts)[0], "BLOCKED: mitigation rejected by operator:local-operator.");
});

// --- while it runs -----------------------------------------------------------------------------------------

test("waiting at the approval: the approval step waits, the rest has not happened yet", () => {
  const events = until(APPROVED, (e) => e.category === "approval.requested");
  const progress = demoProgress(events);
  assert.equal(progress.finished, false);
  assert.equal(progress.current?.id, "approve");
  assert.equal(progress.current?.status, "waiting");
  assert.deepEqual(
    progress.steps.map((s) => s.status),
    ["done", "done", "done", "done", "done", "done", "waiting", "upcoming", "upcoming", "upcoming"],
  );
  assert.deepEqual(texts(progress.current?.facts ?? []), ["Waiting for a person's decision."]);
  const actors = activeActors(events, progress);
  assert.deepEqual(
    actors.map((a) => [a.id, a.kind]),
    [["operator", "person"]],
  );
});

test("mid-investigation: the running agents are named, with what each did last", () => {
  const events = until(APPROVED, (e) => e.category === "tool.call" && e.seq > 20);
  const progress = demoProgress(events);
  assert.equal(progress.current?.id, "investigate");
  assert.equal(progress.current?.status, "current");
  assert.equal(progress.current?.tone, "accent");
  assert.deepEqual(
    progress.steps.slice(4).map((s) => s.status),
    ["upcoming", "upcoming", "upcoming", "upcoming", "upcoming", "upcoming"],
  );
  const actors = activeActors(events, progress);
  assert.deepEqual(
    actors.map((a) => a.name),
    ["Telemetry investigator", "Infrastructure investigator"],
  );
  for (const actor of actors) {
    assert.equal(actor.kind, "agent");
    assert.ok(actor.doing, actor.name);
  }
});

test("a second round makes the strategy step current again; round one's verdict stays visible", () => {
  const events = until(APPROVED, (e) => e.category === "routing.decision" && (e.payload as { round?: number }).round === 2);
  const progress = demoProgress(events);
  assert.equal(progress.round, 2);
  assert.equal(progress.current?.id, "strategy");
  const verify = progress.steps.find((s) => s.id === "verify");
  assert.equal(verify?.status, "done");
  assert.equal(verify?.tone, "warn");
});

test("the run's end is known without run.summary when the stream says so", () => {
  const events = until(APPROVED, (e) => e.category === "approval.requested");
  const progress = demoProgress(events, true);
  assert.equal(progress.finished, true);
  assert.equal(progress.steps.find((s) => s.id === "act")?.status, "skipped");
});

// --- activity, with provenance --------------------------------------------------------------------------

test("model activity says it is a replayed recording, never fresh inference", () => {
  const activity = recentActivity(APPROVED, 50);
  const models = activity.filter((a) => a.kind === "model");
  assert.ok(models.length > 0);
  for (const item of models) {
    assert.equal(item.replayed, true);
    assert.match(item.text, /^Recorded answer of qwen3:8b \(replayed, not called now\): /);
    // A refused answer also says, in RECAP's words, why it could not be used.
    assert.match(item.text, /originally took \d+\.\d s\.( RECAP could not use it \(.+\) and asked again\.)?$/);
  }
  assert.equal(models.filter((m) => m.text.includes("RECAP could not use it")).length, 1);
  const tools = activity.filter((a) => a.kind === "tool");
  assert.ok(tools.every((t) => /evidence ev-/.test(t.detail)));
  // Read ALLOWs are noise; the approval the policy asked for is not.
  const policy = activity.filter((a) => a.kind === "policy");
  assert.deepEqual(
    policy.map((p) => p.text),
    ["The safety policy asked for a person's approval of restart_service."],
  );
  assert.equal(recentActivity(APPROVED).length, 6);
});

test("tools and fixes in plain words", () => {
  assert.equal(toolWords("get_service_metrics", { service: "frontend", metric: "error_rate", window_minutes: 60 }), "read frontend's error_rate metric (last 60 min)");
  assert.equal(toolWords("get_k8s_like_events", { service: "inventory" }), "read the cluster events for inventory");
  assert.equal(toolWords("get_recent_deployments", {}), "read the recent deployments");
  assert.equal(toolWords("brand_new_tool", {}), "called brand_new_tool");
  assert.equal(actionWords("execute_rollback", "checkout", { to_version: "v1.4.2" }), "roll checkout back to v1.4.2");
  assert.equal(actionWords("restart_service", "inventory"), "restart the inventory service");
});

// --- problems are shown, not hidden ------------------------------------------------------------------------

test("refusals, failed tools, a rejected fix, an expired approval and no recovery each become a notice", () => {
  let seq = 1000;
  const at = (stage: string, category: string, payload: Record<string, unknown>, agent: string | null = null): TraceEvent => ({
    event_id: `evt-${seq}`,
    run_id: "run-x",
    seq: seq++,
    stage,
    ts: "2026-11-08T00:00:00.000000+00:00",
    agent,
    category,
    payload,
    duration_ms: null,
    usage: null,
    schema_version: "recap.v1",
  });
  const notices = demoNotices([
    at("INVESTIGATING", "tool.result", { tool: "get_service_logs", ok: false, error: "timeout after 10 s" }, "telemetry_investigator"),
    at("INVESTIGATING", "policy.decision", { tool: "delete_namespace", verdict: "DENY", rule_ids: ["R1-tool-name-format", "R9-destructive-deny"] }, "controller"),
    at("PROPOSING", "verification.evidence", { proposal_check: { ok: false, problems: ["the rollback target is not a known release"], rule_ids: ["VP4-restore-point"] } }),
    at("AWAITING_APPROVAL", "approval.decided", { decision: "expired" }),
    at("VERIFYING", "verification.recovery", { verified: false, unhealthy_after: ["checkout"] }),
    at("BUDGET_EXHAUSTED", "state.transition", { to: "BUDGET_EXHAUSTED", reason: "tool-call budget exhausted" }),
  ]);
  assert.deepEqual(
    notices.map((n) => [n.title, n.tone, n.codes]),
    [
      ["1 tool call failed", "warn", []],
      ["The safety policy refused delete_namespace", "bad", ["R9-destructive-deny"]],
      ["The evidence gate rejected the proposed fix", "bad", ["VP4-restore-point"]],
      ["The approval request expired", "warn", []],
      ["Recovery was not verified", "bad", []],
      ["A run limit was reached", "warn", []],
    ],
  );
});

test("a model answer RECAP refused is shown, with RECAP's reason and whether the retry was usable", () => {
  // In the golden recording the infrastructure agent once asked to repeat a tool call it had made.
  for (const events of [APPROVED, REJECTED]) {
    const notices = demoNotices(events);
    assert.equal(notices.length, 1);
    assert.equal(notices[0].title, "RECAP refused 1 model answer and asked again");
    assert.equal(notices[0].tone, "info");
    assert.match(notices[0].text, /^Infrastructure investigator: dropped a repeat of mcp:get_k8s_like_events\(limit=5,service=inventory\)\. The next attempt was usable\.$/);
  }
});

// --- the approval, the recording, the address --------------------------------------------------------------

test("the approval says what happens either way, and that nothing real changes in the simulation", () => {
  const story = approvalStory({ action: "restart_service", target: "inventory", params: {} }, true, "reversible_write");
  assert.equal(story.headline, "Restart the inventory service");
  assert.match(story.ifApprove, /^RECAP runs restart_service on inventory once, through the safety gateway, against RECAP's simulated shop\. Nothing real changes\./);
  assert.equal(story.ifReject, "Nothing runs. The run ends as blocked, and your decision is logged.");
  assert.match(story.risk, /^A reversible change/);
  assert.match(approvalStory({ action: "restart_service", target: "geo" }, false).ifApprove, /against the live target/);
});

test("provenance names the recorded model and day, and says no model is called now", () => {
  assert.equal(recordedOn("20261025-121828-ollama-6a5cfd"), "2026-10-25");
  assert.equal(recordedOn("not-a-date"), "");
  const words = provenanceWords(fixture());
  assert.match(words, /answers qwen3:8b \(served by ollama\) gave in a real run on 2026-10-25/);
  assert.match(words, /no model is called now/);
  assert.match(words, /Nothing real is changed\.$/);
});

test("the golden recording is preferred, and only a launchable one", () => {
  const other = fixture({ id: "qwen3-8b-bad-deployment", tags: ["single-agent"] });
  assert.equal(goldenFixture([other, fixture()])?.id, "qwen3-8b-unfamiliar-parallel");
  assert.equal(goldenFixture([other, fixture({ launchable: false })])?.id, "qwen3-8b-bad-deployment");
  assert.equal(goldenFixture([fixture({ launchable: false })]), null);
});

test("paces change only the waiting, and the address keeps the run and interview mode", () => {
  assert.deepEqual(
    DEMO_PACES.map((p) => [p.key, p.value]),
    [
      ["guided", 0.25],
      ["quick", 0.05],
      ["instant", 0],
    ],
  );
  assert.equal(paceFor("instant").value, 0);
  assert.equal(paceFor("nonsense").key, "guided");
  assert.equal(demoPageHref(), "#/demo");
  assert.equal(demoPageHref({ run: "run-abc", interview: true }), "#/demo?run=run-abc&mode=interview");
});

// --- rule codes in plain English ----------------------------------------------------------------------------

test("a rejected restore point reads as the specification's example sentence", () => {
  const rule = explainRule("VP4-restore-point");
  assert.equal(rule.family, "Evidence gate");
  assert.equal(rule.failed, "Proposed rollback rejected: the selected revision is not a verified safe restore point.");
  assert.equal(ruleTitle("VP4-restore-point"), `Evidence gate: ${rule.checks}`);
});

test("every rule RECAP records in a real replay is explained", () => {
  const codes = new Set<string>();
  for (const event of APPROVED) {
    const payload = event.payload as { rule_ids?: string[]; rule_id?: string; proposal_check?: { rule_ids?: string[] } };
    for (const code of [...(payload.rule_ids ?? []), ...(payload.proposal_check?.rule_ids ?? []), payload.rule_id ?? ""]) if (code) codes.add(code);
  }
  assert.ok(codes.size >= 15);
  for (const code of codes) assert.ok(isExplained(code), code);
});

test("an unknown code stands for itself; a new suffix on a known number keeps its meaning", () => {
  assert.equal(explainRule("R11-renamed-later").family, "Safety policy");
  assert.equal(explainRule("R11-renamed-later").code, "R11-renamed-later");
  // V1 has three rules, so a new V1 code is not guessed.
  assert.equal(explainRule("V1-something-new").family, "Rule");
  assert.equal(explainRule("X9").checks, "Rule X9.");
  assert.equal(explainRule(null).checks, "No rule recorded.");
});
