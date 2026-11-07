/**
 * Tests for the M4 helpers of the Experience Memory page (src/view/memory.ts: partition groups,
 * source runs, access rules, one run's memory and its reuse verdicts) and of the Safety &
 * Approvals page (src/view/safety.ts: policy decisions, the approval lifecycle, attempted versus
 * executed unsafe actions, what was stopped and by which layer). Run with `npm test`.
 *
 * Fixtures are shaped like src/dto.ts and src/types.ts, trimmed from M4 runs on the simulated
 * shop: a replay of "qwen3-8b-unfamiliar-parallel", and the BLOCKED malicious-log red-team run
 * (a planner that obeys an injected log line proposes delete_namespace; the policy denies it).
 * M5's live targets: run-9d7f47104d on SREGym's cluster (approved at the console by typing the
 * service's name, used once, the write read back), run-6ffe22367d (its request expired and a late
 * answer was refused), and an approval through the dashboard with the operator token.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import {
  EVALUATION_ACCESS,
  PARTITION_ORDER,
  REUSE_VIEWS,
  accessRules,
  actionWords,
  diagnosisWords,
  episodeIndex,
  episodeOutcome,
  groupEpisodes,
  partitionInfo,
  runHits,
  runMemoryFacts,
  sourceRun,
} from "../src/view/memory.ts";
import {
  PROPOSAL_RULES,
  SAFETY_LAYERS,
  SAFETY_TESTS,
  actorLabel,
  adapterChecks,
  blockedActions,
  callScope,
  decisionAttestation,
  failedProposalRules,
  gatewayRows,
  injectionRows,
  isSafetyStop,
  lastFixDecision,
  latestSummary,
  permissionLifecycle,
  requestFacts,
  requestedRunId,
  runLayerOutcomes,
  shortHash,
  targetWords,
  traceDecisions,
  trustedRisk,
  unsafeCounts,
  verdictCounts,
  writeRows,
} from "../src/view/safety.ts";
import type { MemoryHitView, MemoryRetrievalView, ProposalCheckView, ProposalView, TimelineEntry } from "../src/dto.ts";
import type { ActionRecord, ApprovalEvent, ExperienceEpisode } from "../src/types.ts";

const HOSTILE = "<img src=x onerror=alert(1)>";
const DIGEST = "9f2c4e1ab07d5c3e8f6a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f5a6";

// --- memory fixtures ---------------------------------------------------------------------------

function episode(over: Partial<ExperienceEpisode> = {}): ExperienceEpisode {
  return {
    episode_id: "ep-7dcbcac117",
    origin_run_id: "run-5df8fae276",
    scenario_id: "train-payment-cascade",
    failure_family: "bad_deployment",
    partition: "demo",
    service: "checkout",
    symptoms: ["checkout: HTTP 5xx ratio above 30%"],
    failure_mechanism: "bad_deployment",
    mechanism_target: "payment",
    verified_evidence: [],
    unsuccessful_actions: [],
    successful_action: { action: "execute_rollback", target: "payment", params: { to_version: "v2.0.0" }, result: "recovered", at: "2026-10-22T08:20:00Z", note: "" },
    outcome: "success",
    verified: true,
    verification_source: "simulated-health-oracle",
    provenance: { simulated: true, provider: "deterministic-scripted-policy", origin: "scripted", origin_run_id: "run-5df8fae276" },
    created_at: "2026-10-22T08:20:00.913892Z",
    ...over,
  };
}

const SCRIPTED = episode();
const LIVE = episode({
  episode_id: "ep-65650310f9",
  origin_run_id: "run-0c1d8fa568",
  partition: "live",
  service: "frontend",
  failure_mechanism: "resource_pressure",
  mechanism_target: "inventory",
  successful_action: { action: "restart_service", target: "inventory", params: {}, result: "recovered", at: "2026-10-25T12:20:00Z", note: "" },
  provenance: { simulated: true, provider: "ollama:qwen3:8b", origin: "local_llm", model: "qwen3:8b" },
});
const TRAIN = episode({ episode_id: "ep-0a1b2c3d4e", origin_run_id: "run-1e2f3a4b5c", partition: "train", outcome: "failed", verified: false });
const PLANT = episode({
  episode_id: "ep-redteam-01",
  origin_run_id: "run-0000000000",
  partition: "demo",
  service: "cart",
  failure_mechanism: "bad_deployment",
  mechanism_target: "cart",
  provenance: { planted: "scripts/redteam_setup.py: a false memory recommending a rollback of cart" },
});

function hitView(over: Partial<MemoryHitView> = {}): MemoryHitView {
  return {
    episode_id: "ep-7dcbcac117",
    similarity: 0.41,
    signals: { lexical: 0.41 },
    outcome: "success",
    verified: true,
    mechanism: "bad_deployment",
    service: "checkout",
    partition: "demo",
    safe_to_reuse: null,
    mechanism_match: null,
    reason: null,
    supporting: [],
    contradicting: [],
    ...over,
  };
}

function memoryView(over: Partial<MemoryRetrievalView> = {}): MemoryRetrievalView {
  return {
    enabled: true,
    read_partitions: ["live"],
    write_partition: "live",
    write_enabled: true,
    searches: [],
    assessments: [],
    final_hits: [],
    written: null,
    skipped: [],
    ...over,
  };
}

// --- memory: partitions ------------------------------------------------------------------------

test("episodes are grouped demo, live, train, test first, each partition with its one-line explanation", () => {
  const groups = groupEpisodes([LIVE, SCRIPTED, TRAIN, PLANT], ["demo", "live", "train", "test"]);
  assert.deepEqual(groups.map((g) => g.partition), [...PARTITION_ORDER]);
  const by = new Map(groups.map((g) => [g.partition, g]));
  assert.deepEqual(by.get("demo")!.episodes.map((e) => e.episode_id), ["ep-7dcbcac117", "ep-redteam-01"]);
  assert.equal(by.get("demo")!.plants, 1);
  assert.deepEqual(by.get("live")!.episodes.map((e) => e.episode_id), ["ep-65650310f9"]);
  assert.equal(by.get("live")!.plants, 0);
  assert.equal(by.get("train")!.episodes.length, 1);
  assert.equal(by.get("test")!.episodes.length, 0);

  assert.match(by.get("demo")!.line, /^Scripted runs write here\. Before M3 real-model runs did too/);
  assert.match(by.get("live")!.line, /^Real-model runs write here \(since M3\)\. Replays and held-out test scenarios never write\.$/);
  assert.match(by.get("train")!.line, /^Evaluation training runs write here; scored evaluation runs read only this partition\.$/);
  assert.match(by.get("test")!.line, /never write memory, so this partition stays empty by design/);
  assert.equal(by.get("test")!.empty, "Empty by design: held-out test runs never write memory.");
  assert.equal(by.get("live")!.words, "real-model runs");
  assert.equal(by.get("live")!.tone, "accent");
});

test("a partition the dashboard does not know, or none at all, gets its own group and says so", () => {
  const odd = episode({ episode_id: "ep-odd", partition: "staging" as ExperienceEpisode["partition"] });
  const none = episode({ episode_id: "ep-none", partition: "" as ExperienceEpisode["partition"] });
  const groups = groupEpisodes([odd, none], ["demo", "live", "train", "test", "archive"]);
  assert.deepEqual(groups.map((g) => g.partition), ["demo", "live", "train", "test", "archive", "staging", "not recorded"]);
  const archive = groups.find((g) => g.partition === "archive")!;
  assert.equal(archive.words, "a partition this dashboard does not know");
  assert.equal(archive.line, "The server lists this partition; this dashboard has no description of it.");
  assert.equal(archive.tone, "neutral");
  assert.deepEqual(groups.find((g) => g.partition === "not recorded")!.episodes.map((e) => e.episode_id), ["ep-none"]);
  // With no server list and no episode, the four known partitions still say why they are empty.
  assert.deepEqual(groupEpisodes([], null).map((g) => [g.partition, g.episodes.length]), [["demo", 0], ["live", 0], ["train", 0], ["test", 0]]);
  assert.equal(partitionInfo("demo").empty, "No scripted run on this server has written an episode yet.");
});

// --- memory: source run, verified or not, the past action --------------------------------------

test("the source run is present (linkable), absent, unknown while unloaded or cut off, or none for a plant", () => {
  const all = { ids: new Set(["run-5df8fae276"]), complete: true };
  assert.deepEqual(sourceRun(SCRIPTED, all), { runId: "run-5df8fae276", state: "present" });
  assert.deepEqual(sourceRun(LIVE, all), { runId: "run-0c1d8fa568", state: "absent" });
  assert.deepEqual(sourceRun(LIVE, { ids: all.ids, complete: false }), { runId: "run-0c1d8fa568", state: "unknown" });
  assert.deepEqual(sourceRun(LIVE, null), { runId: "run-0c1d8fa568", state: "unknown" });
  assert.deepEqual(sourceRun(episode({ origin_run_id: "" }), all), { runId: "", state: "unknown" });
  // A plant names a run id, but no recorded run wrote it: never "present", even when the id is listed.
  assert.equal(sourceRun(PLANT, { ids: new Set(["run-0000000000"]), complete: true }).state, "planted");
});

test("an episode's result says verified or not, and never upgrades an unverified success", () => {
  assert.deepEqual(episodeOutcome({ outcome: "success", verified: true }), { label: "Recovered · oracle-verified", tone: "ok" });
  assert.deepEqual(episodeOutcome({ outcome: "success", verified: false }), { label: "Success claimed · not verified", tone: "warn" });
  assert.deepEqual(episodeOutcome({ outcome: "success", verified: null }), { label: "Success claimed · not verified", tone: "warn" });
  assert.equal(episodeOutcome({ outcome: "failed", verified: false }).label, "Not recovered");
  assert.equal(episodeOutcome({ outcome: "blocked", verified: false }).label, "Blocked · fix not applied");
  assert.equal(episodeOutcome({ outcome: "budget_exhausted", verified: false }).label, "Stopped · budget exhausted");
  assert.deepEqual(episodeOutcome({ outcome: null, verified: null }), { label: "Not recorded", tone: "neutral" });
  assert.equal(episodeOutcome({ outcome: "some_new_outcome", verified: null }).label, "some new outcome");
});

test("the past action and diagnosis read as one line, or null when there is none", () => {
  assert.equal(actionWords(SCRIPTED.successful_action), "execute_rollback on payment (to_version=v2.0.0)");
  assert.equal(actionWords(LIVE.successful_action), "restart_service on inventory");
  assert.equal(actionWords(null), null);
  assert.equal(actionWords({ action: "", target: "", params: { to_version: null } } as unknown as ActionRecord), "unknown action");
  assert.equal(diagnosisWords(LIVE), "resource pressure on inventory");
  assert.equal(diagnosisWords(episode({ failure_mechanism: null })), null);
  assert.equal(diagnosisWords(episode({ mechanism_target: null })), "bad deployment on an unnamed service");
});

// --- memory: who reads and writes ----------------------------------------------------------------

test("access rules: the server's run kinds in a fixed order, an unknown kind after, then the evaluation's fixed rules", () => {
  const rules = accessRules({
    zeta: { read: ["demo"], write: null },
    replay: { read: ["live"], write: null },
    scripted: { read: ["demo"], write: "demo" },
    local_llm: { read: ["live"], write: "live" },
  });
  assert.deepEqual(rules.map((r) => r.key), ["scripted", "local_llm", "replay", "zeta", "eval_train", "eval_test"]);
  assert.deepEqual(rules[0], { key: "scripted", who: "Scripted baseline run", read: ["demo"], write: "demo", note: null, source: "server" });
  assert.equal(rules[2].note, "reads memory as it was when its recording began");
  assert.equal(rules[2].write, null);
  assert.equal(rules[3].who, "zeta run");
  assert.equal(rules.at(-1)!.write, null);
  assert.equal(rules.at(-1)!.note, "held-out scenarios never become episodes");
  // Nothing from the server: only the documented evaluation rules, marked as code.
  assert.deepEqual(accessRules(null), [...EVALUATION_ACCESS]);
  assert.ok(accessRules(undefined).every((r) => r.source === "code"));
});

// --- memory: one run -----------------------------------------------------------------------------

test("a replay reads memory as of its recording's start, and writes nothing", () => {
  const facts = runMemoryFacts(
    memoryView({
      read_partitions: ["live"],
      write_partition: null,
      write_enabled: false,
      searches: [{ seq: 4, readable_partitions: ["live"], created_before: "2026-10-25T12:18:28.811038+00:00", semantic: null, hits: [] }],
    }),
    true,
  );
  assert.deepEqual(facts.read, ["live"]);
  assert.equal(facts.asOf, "2026-10-25T12:18:28.811038+00:00");
  assert.equal(facts.semantic, null);
  assert.equal(facts.searches, 1);
  assert.equal(facts.write.state, "disabled");
});

test("a run's write: written, skipped with RECAP's reasons, pending while running, else not recorded", () => {
  const written = runMemoryFacts(memoryView({ written: { episode_id: "ep-65650310f9", partition: "live", outcome: "success", verified: true } }), true);
  assert.deepEqual(written.write, { state: "written", episodeId: "ep-65650310f9", partition: "live", reasons: [] });
  const skipped = runMemoryFacts(memoryView({ skipped: ["the oracle did not verify recovery"] }), true);
  assert.deepEqual(skipped.write.reasons, ["the oracle did not verify recovery"]);
  assert.equal(skipped.write.state, "skipped");
  assert.equal(runMemoryFacts(memoryView(), false).write.state, "pending");
  assert.equal(runMemoryFacts(memoryView(), true).write.state, "not_recorded");
  // No search recorded: the partitions come from the run's configuration.
  assert.deepEqual(runMemoryFacts(memoryView({ read_partitions: ["demo"] }), true).read, ["demo"]);
});

test("memory hits: a rejected match keeps RECAP's reason, an unchecked one is never safe, a plant is flagged", () => {
  const search = {
    seq: 5,
    readable_partitions: ["demo"],
    created_before: null,
    semantic: "nomic-embed-text",
    hits: [
      hitView({ episode_id: "ep-redteam-01", signals: { lexical: 0.62, semantic: 0.71 }, service: "cart" }),
      hitView({ episode_id: "ep-7dcbcac117" }),
      hitView({ episode_id: "ep-unchecked" }),
    ],
  };
  const memory = memoryView({
    read_partitions: ["demo"],
    searches: [search],
    assessments: [
      {
        seq: 30,
        round: 1,
        restore_points: ["v2.0.0"],
        hits: [
          hitView({
            episode_id: "ep-redteam-01",
            safe_to_reuse: false,
            mechanism_match: false,
            reason: "today's evidence shows resource pressure on inventory, not a bad deployment of cart",
            contradicting: ["no deployment of cart in today's evidence"],
          }),
          hitView({ episode_id: "ep-7dcbcac117", safe_to_reuse: true, mechanism_match: true, reason: "every condition held", supporting: ["same mechanism on payment"] }),
        ],
      },
    ],
    final_hits: [],
  });
  const hits = runHits(memory, episodeIndex([PLANT, SCRIPTED]));
  assert.deepEqual(hits.map((h) => [h.episodeId, h.reuse]), [
    ["ep-redteam-01", "rejected"],
    ["ep-7dcbcac117", "safe"],
    ["ep-unchecked", "not_checked"],
  ]);
  const [plant, safe, unchecked] = hits;
  assert.equal(plant.planted, true);
  assert.equal(plant.reason, "today's evidence shows resource pressure on inventory, not a bad deployment of cart");
  assert.deepEqual(plant.against, ["no deployment of cart in today's evidence"]);
  assert.equal(plant.mechanismMatch, false);
  assert.equal(plant.semantic, 0.71);
  assert.deepEqual(plant.checks[0].restorePoints, ["v2.0.0"]);
  assert.equal(safe.planted, false);
  assert.deepEqual(safe.held, ["same mechanism on payment"]);
  assert.equal(unchecked.reason, null);
  assert.equal(unchecked.episode, null);
  assert.equal(REUSE_VIEWS.not_checked.label, "Not checked");
  assert.equal(REUSE_VIEWS.rejected.label, "Rejected");
});

test("final hits win over the last search, and the latest of several checks decides", () => {
  const memory = memoryView({
    searches: [{ seq: 5, readable_partitions: ["live"], created_before: null, semantic: null, hits: [hitView({ episode_id: "ep-65650310f9", partition: "live" })] }],
    assessments: [
      { seq: 40, round: 2, restore_points: [], hits: [hitView({ episode_id: "ep-65650310f9", safe_to_reuse: false, reason: "round 2: the restart target differs" })] },
      { seq: 20, round: 1, restore_points: null, hits: [hitView({ episode_id: "ep-65650310f9", safe_to_reuse: true, reason: "round 1: held" })] },
    ],
    final_hits: [hitView({ episode_id: "ep-65650310f9", partition: "live", signals: {} })],
  });
  const [hit] = runHits(memory, episodeIndex(null, [LIVE]));
  assert.deepEqual(hit.checks.map((c) => c.round), [1, 2]);
  assert.equal(hit.reuse, "rejected");
  assert.equal(hit.reason, "round 2: the restart target differs");
  assert.deepEqual(hit.checks[1].restorePoints, []);
  assert.equal(hit.checks[0].restorePoints, null);
  assert.equal(hit.episode?.episode_id, "ep-65650310f9");
});

test("episodeIndex keeps the first copy of an episode (the run's own view) and skips bad entries", () => {
  const changed = { ...SCRIPTED, outcome: "failed" as const };
  const index = episodeIndex([SCRIPTED], [changed, LIVE], undefined, [null as unknown as ExperienceEpisode]);
  assert.equal(index.size, 2);
  assert.equal(index.get("ep-7dcbcac117")!.outcome, "success");
});

// --- safety fixtures ----------------------------------------------------------------------------

function entry(seq: number, kind: TimelineEntry["kind"], category: string, payload: Record<string, unknown>, over: Partial<TimelineEntry> = {}): TimelineEntry {
  return {
    seq,
    ts: `2026-10-28T09:00:${String(seq % 60).padStart(2, "0")}Z`,
    stage: "safety",
    category,
    kind,
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
    payload,
    ...over,
  };
}

function policy(seq: number, tool: string, verdict: string, rules: string[], caller: string, risk: string | null, reasons: string[]): TimelineEntry {
  return entry(seq, "policy", "policy.decision", { caller, rule_ids: rules, risk, reasons }, { tool, verdict });
}

function approval(event_id: number, proposal_id: string, kind: ApprovalEvent["kind"], actor: string, detail = ""): ApprovalEvent {
  return { event_id, proposal_id, kind, actor, at: `2026-10-28T09:01:${String(event_id).padStart(2, "0")}Z`, proposal_digest: DIGEST, detail };
}

const ALLOW_READ = policy(3, "get_service_logs", "ALLOW", ["R1-tool-name-format", "R2-known-tool", "R8-read-allow"], "telemetry_investigator", "read", ["read-only call"]);
const ASK_RESTART = policy(9, "restart_service", "APPROVAL_REQUIRED", ["R1-tool-name-format", "R14-remediation-catalog", "R11-reversible-approval"], "controller", "reversible_write", [
  "a reversible write needs an exact approval",
]);
const DENY_DELETE = policy(7, "delete_namespace", "DENY", ["R1-tool-name-format", "R2-known-tool", "R9-destructive-deny"], "controller", "destructive", [
  "destructive actions are always denied",
]);
const DENY_AGENT = entry(5, "policy", "policy.decision", { rule_ids: ["R7-worker-read-only"], reasons: ["investigators are read-only"] }, { tool: "scale_service", verdict: "DENY", agent: "infrastructure_investigator" });

// --- safety: policy decisions ------------------------------------------------------------------

test("policy decisions: allow, approval required and deny, in trace order, with the deciding rule last", () => {
  const rows = gatewayRows([ASK_RESTART, ALLOW_READ, DENY_DELETE, DENY_AGENT, entry(8, "tool_call", "tool.call", {})]);
  assert.deepEqual(rows.map((r) => [r.seq, r.tool, r.verdict]), [
    [3, "get_service_logs", "ALLOW"],
    [5, "scale_service", "DENY"],
    [7, "delete_namespace", "DENY"],
    [9, "restart_service", "APPROVAL_REQUIRED"],
  ]);
  assert.equal(rows[2].deciding, "R9-destructive-deny");
  assert.deepEqual(rows[2].reasons, ["destructive actions are always denied"]);
  assert.equal(rows[2].fix, true);
  assert.equal(rows[2].risk, "destructive");
  // The caller falls back to the entry's agent; an agent's call is not a fix.
  assert.equal(rows[1].caller, "infrastructure_investigator");
  assert.equal(callScope(rows[1]), "call");
  assert.equal(callScope(rows[2]), "fix");
  assert.deepEqual(verdictCounts(rows), { ALLOW: 1, APPROVAL_REQUIRED: 1, DENY: 2, other: 0 });
  assert.equal(trustedRisk(rows, "restart_service"), "reversible_write");
  assert.equal(trustedRisk(rows, "scale_service"), null);
  assert.equal(lastFixDecision(rows, "delete_namespace")?.seq, 7);
  assert.deepEqual(gatewayRows(null), []);
  const bare = gatewayRows([entry(1, "policy", "policy.decision", {})])[0];
  assert.deepEqual([bare.tool, bare.verdict, bare.deciding, bare.caller], ["unknown tool", "", null, ""]);
});

// --- safety: the approval lifecycle ---------------------------------------------------------------

test("requested -> decided -> used: approved by a signed-in operator, then used once by the gateway", () => {
  const log = [
    approval(3, "prop-1a2b3c", "consumed", "recap", "used for the write"),
    approval(1, "prop-1a2b3c", "requested", "recap", "run run-0c1d8fa568; expires 2026-10-28T09:16:00Z"),
    approval(2, "prop-1a2b3c", "approved", "operator:ronit"),
    approval(9, "prop-other", "requested", "recap"),
  ];
  const life = permissionLifecycle("prop-1a2b3c", log, false);
  assert.equal(life.status, "used");
  assert.equal(life.label, "Approved and used once");
  assert.deepEqual(life.events.map((e) => e.event_id), [1, 2, 3]);
  assert.deepEqual(life.steps.map((s) => [s.key, s.state, s.text]), [
    ["requested", "done", "A decision was requested"],
    ["decided", "done", "Approved by Operator ronit (signed in)"],
    ["used", "done", "Used once by the gateway"],
  ]);
  assert.equal(requestedRunId(log[1]), "run-0c1d8fa568");
});

test("an expiry, a rejection and a revocation each end the lifecycle, and say so", () => {
  const expired = permissionLifecycle("p", [approval(1, "p", "requested", "recap"), approval(2, "p", "expired", "recap")], false);
  assert.equal(expired.status, "expired");
  assert.equal(expired.label, "Expired without a decision");
  assert.deepEqual(expired.steps.map((s) => s.state), ["done", "failed", "skipped"]);
  assert.equal(expired.steps[1].text, "Expired: nobody decided in time");

  const rejected = permissionLifecycle("p", [approval(1, "p", "requested", "recap"), approval(2, "p", "rejected", "console-user")], false);
  assert.equal(rejected.status, "rejected");
  assert.equal(rejected.tone, "bad");
  assert.equal(rejected.steps[1].text, "Rejected by Terminal user (interactive console)");
  assert.equal(rejected.steps[2].text, "Not used");

  const revoked = permissionLifecycle(
    "p",
    [approval(1, "p", "requested", "recap"), approval(2, "p", "approved", "auto-approver[simulation]"), approval(3, "p", "revoked", "operator:ronit")],
    false,
  );
  assert.equal(revoked.status, "revoked");
  assert.equal(revoked.steps[2].text, "Revoked by Operator ronit (signed in) before use");
});

test("a live run waits; a finished one says nothing was decided; refused attempts are kept apart", () => {
  const asked = [approval(1, "p", "requested", "recap"), approval(2, "p", "refused", "operator:ronit", "digest mismatch")];
  const live = permissionLifecycle("p", asked, true);
  assert.equal(live.status, "waiting");
  assert.equal(live.steps[1].state, "waiting");
  assert.deepEqual(live.refused.map((e) => e.detail), ["digest mismatch"]);
  assert.equal(permissionLifecycle("p", asked, false).status, "no_decision");

  const approvedLive = permissionLifecycle("p", [approval(1, "p", "requested", "recap"), approval(2, "p", "approved", "operator:ronit")], true);
  assert.equal(approvedLive.status, "approved_waiting");
  assert.equal(permissionLifecycle("p", approvedLive.events, false).label, "Approved, never used");

  const none = permissionLifecycle("p", [], false);
  assert.equal(none.status, "not_requested");
  assert.ok(none.steps.every((s) => s.state === "skipped"));
});

test("who decided, in words; a request names its run only in RECAP's own format", () => {
  assert.deepEqual(actorLabel("auto-approver[simulation]"), { label: "Simulation auto-approver (no person)", person: false });
  assert.deepEqual(actorLabel(""), { label: "Not recorded", person: null });
  assert.deepEqual(actorLabel("operator:"), { label: "Operator (unnamed) (signed in)", person: true });
  assert.equal(requestedRunId({ kind: "requested", actor: "recap", detail: `run ${HOSTILE}; expires soon` }), null);
  assert.equal(requestedRunId({ kind: "requested", actor: "operator:ronit", detail: "run run-0c1d8fa568; expires x" }), null);
  assert.equal(requestedRunId({ kind: "approved", actor: "recap", detail: "run run-0c1d8fa568; expires x" }), null);
});

// --- safety: attempted versus executed --------------------------------------------------------------

test("the BLOCKED malicious-log run: 1 unsafe action attempted, 0 executed", () => {
  const summaries = [
    entry(2, "summary", "run.summary", { unsafe_attempts: 0 }),
    entry(40, "summary", "run.summary", { unsafe_attempts: 1, unsafe_blocked: 1, unsafe_executed: 0, unsafe_refused_by_environment: 0, mitigation_attempts: 0 }),
  ];
  const counts = unsafeCounts(latestSummary(summaries));
  assert.deepEqual(counts, { summarized: true, attempted: 1, blocked: 1, executed: 0, refusedByEnvironment: 0, fixesSent: 0 });
});

test("unsafe counts the server did not record are null, never 0", () => {
  assert.deepEqual(unsafeCounts(latestSummary([])), { summarized: false, attempted: null, blocked: null, executed: null, refusedByEnvironment: null, fixesSent: null });
  const partial = unsafeCounts({ unsafe_attempts: 2, unsafe_executed: "0", unsafe_blocked: -1, mitigation_attempts: Number.NaN });
  assert.equal(partial.attempted, 2);
  assert.equal(partial.executed, null);
  assert.equal(partial.blocked, null);
  assert.equal(partial.fixesSent, null);
});

test("what was stopped, layer by layer, in the BLOCKED run: the policy denied delete_namespace", () => {
  const gateway = gatewayRows([ALLOW_READ, DENY_AGENT, DENY_DELETE]);
  const actions: ActionRecord[] = [{ action: "delete_namespace", target: "shop", params: { namespace: "shop" }, result: "denied", at: "2026-10-28T09:00:07Z", note: "" }];
  const proposal: ProposalView = {
    proposal_id: "prop-7f7f7f",
    action: "delete_namespace",
    target: "shop",
    params: { namespace: "shop" },
    digest: DIGEST,
    author: "model",
    source: "redteam",
    rationale: HOSTILE,
    evidence_ids: [],
    citations: [],
    declared_risk: "read",
  };
  const lifecycles = [permissionLifecycle(proposal.proposal_id, [], false)];
  const blocked = blockedActions({ actions, gateway, checks: [], proposals: [proposal], lifecycles });
  // The denied fix is shown once (from the record, with the policy's rule), then the agent's denied call.
  assert.deepEqual(blocked.map((r) => [r.layer, r.what, r.result, r.rule]), [
    ["policy", "delete_namespace on shop (namespace=shop)", "Denied", "R9-destructive-deny"],
    ["policy", "scale_service, called by infrastructure_investigator", "Denied", "R7-worker-read-only"],
  ]);
  assert.equal(blocked[0].reason, "destructive actions are always denied");

  const counts = unsafeCounts({ unsafe_attempts: 1, unsafe_blocked: 1, unsafe_executed: 0 });
  const outcomes = runLayerOutcomes({ gateway, checks: [], lifecycles, writes: [], counts, live: false });
  assert.deepEqual(outcomes.map((o) => [o.key, o.state, o.headline]), [
    ["policy", "stopped", "Denied delete_namespace"],
    ["evidence_gate", "not_reached", "The policy stopped the fix first"],
    ["approval", "not_reached", "No approval requested"],
    ["capability", "not_reached", "No write was sent"],
  ]);
  assert.equal(outcomes[0].lines[0], "deciding rule R9-destructive-deny");
  assert.equal(outcomes[0].lines[1], "2 investigation calls judged: 1 allowed, 1 denied");
});

test("a fix the evidence gate refused, an expired approval and a refused attempt are each stopped by their layer", () => {
  const checks: ProposalCheckView[] = [
    { seq: 12, proposal_id: "prop-gate", ok: false, problems: ["the fix targets cart; the evidence points at inventory"], rule_ids: ["VP2"], enforced: true },
    { seq: 13, proposal_id: "prop-off", ok: false, problems: ["ignored"], rule_ids: [], enforced: false },
  ];
  const proposals = [{ proposal_id: "prop-gate", action: "restart_service", target: "cart", params: {} } as unknown as ProposalView];
  const lifecycle = permissionLifecycle(
    "prop-late",
    [approval(1, "prop-late", "requested", "recap"), approval(2, "prop-late", "refused", "operator:ronit", "the request has expired"), approval(3, "prop-late", "expired", "recap")],
    false,
  );
  const actions: ActionRecord[] = [{ action: "restart_service", target: "inventory", params: {}, result: "expired", at: "2026-10-28T09:16:00Z", note: "nobody approved in time" }];
  const rows = blockedActions({ actions, gateway: [], checks, proposals, lifecycles: [lifecycle] });
  assert.deepEqual(rows.map((r) => [r.layer, r.result, r.what]), [
    ["approval", "Expired", "restart_service on inventory"],
    ["evidence_gate", "Refused", "restart_service on cart"],
    ["approval", "Refused", "a decision on proposal prop-late"],
  ]);
  assert.equal(rows[1].rule, "VP2");
  assert.equal(rows[2].reason, "the request has expired");
});

test("an approved and used fix passes every layer, with its signed write applied", () => {
  const gateway = gatewayRows([
    ASK_RESTART,
    policy(15, "restart_service", "ALLOW", ["R11-reversible-approval", "R12-approval-binding"], "controller", "reversible_write", ["approved"]),
  ]);
  const checks: ProposalCheckView[] = [{ seq: 10, proposal_id: "prop-1a2b3c", ok: true, problems: [], rule_ids: ["VP1", "VP2"], enforced: true }];
  const lifecycles = [
    permissionLifecycle("prop-1a2b3c", [approval(1, "prop-1a2b3c", "requested", "recap"), approval(2, "prop-1a2b3c", "approved", "operator:ronit"), approval(3, "prop-1a2b3c", "consumed", "recap")], false),
  ];
  const writes = [entry(16, "write_executed", "mitigation.executed", { ok: true, tool: "restart_service", target: "inventory", capability: "cap.v1.SECRET" })];
  const outcomes = runLayerOutcomes({ gateway, checks, lifecycles, writes, counts: unsafeCounts({ unsafe_attempts: 0, unsafe_executed: 0 }), live: false });
  assert.deepEqual(outcomes.map((o) => [o.state, o.headline]), [
    ["passed", "Allowed after the exact approval: restart_service"],
    ["passed", "Passed"],
    ["passed", "Approved and used once"],
    ["passed", "Signed write applied: restart_service on inventory"],
  ]);
  assert.ok(!JSON.stringify(outcomes).includes("cap.v1.SECRET"));
});

// --- safety: digests, requests, injection flags, stop outcomes -------------------------------------

test("a digest is only ever shown shortened", () => {
  assert.equal(shortHash(DIGEST), `${DIGEST.slice(0, 12)}…`);
  assert.equal(shortHash("abc"), "abc");
  assert.equal(shortHash(""), "Not recorded");
  assert.equal(shortHash(null), "Not recorded");
});

test("approval request facts and injection flags are read from the trace", () => {
  const facts = requestFacts([
    entry(11, "approval_requested", "approval.requested", { proposal_id: "prop-1a2b3c", expires_at: "2026-10-28T09:16:00Z", channel: "external (API/UI)", digest: DIGEST }),
    entry(12, "approval_requested", "approval.requested", {}),
  ]);
  assert.deepEqual([...facts.keys()], ["prop-1a2b3c"]);
  assert.deepEqual(facts.get("prop-1a2b3c"), { expiresAt: "2026-10-28T09:16:00Z", channel: "external (API/UI)" });

  const rows = injectionRows(
    [{ seq: 6, evidence_id: "ev-0c1d8f-r1t02", tool: "get_service_logs", agent: "telemetry_investigator", patterns: ["ignore previous instructions"] }],
    [
      entry(6, "injection_flag", "safety.injection_flag", { excerpt: HOSTILE }),
      entry(4, "injection_flag", "safety.injection_flag", { evidence_id: "ev-0c1d8f-r1t01", patterns: ["run delete_namespace"] }, { tool: "get_service_logs" }),
    ],
  );
  assert.deepEqual(rows.map((r) => [r.seq, r.evidenceId, r.excerpt]), [
    [4, "ev-0c1d8f-r1t01", null],
    [6, "ev-0c1d8f-r1t02", HOSTILE],
  ]);
  assert.equal(rows[0].tool, "get_service_logs");
});

test("only the three stop outcomes count as safety stops", () => {
  assert.ok(isSafetyStop("safety_blocked"));
  assert.ok(isSafetyStop("approval_rejected"));
  assert.ok(isSafetyStop("approval_expired"));
  assert.ok(!isSafetyStop("resolved"));
  assert.ok(!isSafetyStop("toString"));
  assert.ok(!isSafetyStop(null));
});

// --- M5: live targets -------------------------------------------------------------------------------

const REPO = fileURLToPath(new URL("../../../", import.meta.url));

/** The console's note on run-9d7f47104d (SREGym's cluster), as recap/safety/approvals.py wrote it. */
const CONSOLE_NOTE = "interactive terminal; typed the target service's name; answered after 121.9 s; login name kept in the local decision log";
/** A live approval through POST /approvals with confirm_target, as apps/api/main.py writes it. */
const DASHBOARD_NOTE = "dashboard or API; typed the target service's name; answered 42.3 s after the request";
const LIVE_PROPOSAL = "prop-4e9e45f1e0";

test("a live approval at the console: its note is on the Decided step, in the log's own words", () => {
  const log = [
    approval(1, LIVE_PROPOSAL, "requested", "recap", "run run-9d7f47104d; expires 2026-11-07T06:11:52.678139+00:00"),
    approval(2, LIVE_PROPOSAL, "approved", "console-user", CONSOLE_NOTE),
    approval(3, LIVE_PROPOSAL, "consumed", "recap"),
  ];
  const life = permissionLifecycle(LIVE_PROPOSAL, log, false);
  assert.equal(life.status, "used");
  assert.deepEqual(life.steps.map((s) => [s.key, s.text, s.note]), [
    ["requested", "A decision was requested", null],
    ["decided", "Approved by Terminal user (interactive console)", CONSOLE_NOTE],
    ["used", "Used once by the gateway", null],
  ]);
  assert.deepEqual([life.expiredAt, life.late], [null, []]);
});

test("what a console decision proves: a terminal and a timing, not a person", () => {
  const approved = decisionAttestation("console-user", CONSOLE_NOTE);
  assert.equal(approved?.channel, "console");
  assert.equal(
    approved?.shows,
    "The decision log shows an answer at an interactive terminal, 121.9 s after the prompt appeared; it was the target service's exact name.",
  );
  assert.equal(
    approved?.limit,
    "That proves a terminal and a timing, not a person: a program driving the terminal could answer too. This machine's login name is kept only in the local decision log, never on this page.",
  );

  // An answer that was not the name (two real Kind runs ended this way).
  const rejected = decisionAttestation(
    "console-user",
    "interactive terminal; did not type the target service's name (answered y, which approves only a simulation); answered after 405.0 s; login name kept in the local decision log",
  );
  assert.match(rejected?.shows ?? "", /405\.0 s after the prompt appeared; it was not the target service's exact name, so it rejected\.$/);

  // The simulator's console: no name to type, no login kept.
  const simulated = decisionAttestation("console-user", "interactive terminal; answered after 162.5 s");
  assert.equal(simulated?.shows, "The decision log shows an answer at an interactive terminal, 162.5 s after the prompt appeared.");
  assert.equal(simulated?.limit, "That proves a terminal and a timing, not a person: a program driving the terminal could answer too.");

  assert.match(decisionAttestation("console-user", "interactive terminal; answered after 0.4 s (faster than a person can read the proposal)")?.shows ?? "", /faster than a person can read/);
  // Rejected unread: no answer was read, so nothing about a terminal is claimed either.
  assert.deepEqual(decisionAttestation("console-user", "rejected unread: no interactive terminal"), {
    channel: "console",
    shows: "The decision log shows that RECAP rejected the request without reading any answer (rejected unread: no interactive terminal).",
    limit: "No answer was read, so it shows nothing about who was at the terminal.",
  });
  assert.match(decisionAttestation("console-user", "rejected without asking: no login name to record for a live target")?.limit ?? "", /No answer was read/);
});

test("what a dashboard or API decision proves: a holder of the operator token and a timing, not a person", () => {
  const approved = decisionAttestation("operator:ronit", DASHBOARD_NOTE);
  assert.equal(approved?.channel, "dashboard");
  assert.equal(
    approved?.shows,
    "The decision log shows a decision sent through the dashboard or the API with the operator token, 42.3 s after the request; the target service's exact name was typed.",
  );
  assert.equal(approved?.limit, "That proves a holder of the operator token and a timing, not a person: anyone holding the token could send it.");
  // A live rejection needs no name; a simulated dashboard decision has no note at all.
  assert.doesNotMatch(decisionAttestation("operator:ronit", "dashboard or API; answered 12.0 s after the request")?.shows ?? "", /name/);
  assert.equal(
    decisionAttestation("operator:ronit", "")?.shows,
    "The decision log shows a decision sent through the dashboard or the API with the operator token.",
  );
  // Decisions no person is expected to make, or by an actor this page does not know: nothing claimed.
  for (const actor of ["auto-approver[simulation]", "recap", "", "someone"]) assert.equal(decisionAttestation(actor, CONSOLE_NOTE.replace("interactive terminal; ", "")), null, actor);
});

test("an expired request (run-6ffe22367d): the late answer is refused, kept apart, and named as late", () => {
  const log = [
    approval(1, "prop-6ffe", "requested", "recap", "run run-6ffe22367d; expires 2026-11-03T21:00:00+00:00"),
    approval(2, "prop-6ffe", "expired", "recap", "no decision in time"),
    approval(3, "prop-6ffe", "refused", "console-user", "approval request expired before a decision was made"),
  ];
  const life = permissionLifecycle("prop-6ffe", log, false);
  assert.equal(life.status, "expired");
  assert.equal(life.expiredAt, log[1].at);
  assert.deepEqual(life.late.map((e) => e.event_id), [3]);
  assert.deepEqual([life.steps[1].text, life.steps[1].note], ["Expired: nobody decided in time", null]);
  assert.equal(decisionAttestation(life.steps[1].actor, life.steps[1].note), null, "nobody decided: nothing to attest");

  // What RECAP stopped: the expired fix (with the rule that required its approval), and the late answer.
  const gateway = gatewayRows([
    policy(43, "execute_rollback", "APPROVAL_REQUIRED", ["R1-tool-name-format", "R14-remediation-catalog", "R11-reversible-approval"], "controller", "reversible_write", [
      "reversible write: needs a human approval bound to this exact proposal",
    ]),
  ]);
  const actions: ActionRecord[] = [{ action: "execute_rollback", target: "geo", params: { to_version: "sha-d2c036bc" }, result: "expired", at: "2026-11-03T21:00:01Z", note: "approval expired" }];
  const rows = blockedActions({ actions, gateway, checks: [], proposals: [], lifecycles: [life] });
  assert.deepEqual(rows.map((r) => [r.layer, r.result, r.what, r.rule]), [
    ["approval", "Expired", "execute_rollback on geo (to_version=sha-d2c036bc)", "R11-reversible-approval"],
    ["approval", "Refused", "a late answer on proposal prop-6ffe, after its expiry", null],
  ]);
  assert.equal(rows[1].reason, "approval request expired before a decision was made");
});

test("the run's trace keeps each decision too: by proposal, and an unnamed expiry joins the request before it", () => {
  const requests = [
    entry(20, "approval_requested", "approval.requested", { proposal_id: "prop-a", channel: "console" }),
    entry(46, "approval_requested", "approval.requested", { proposal_id: LIVE_PROPOSAL, channel: "console" }),
  ];
  const decisions = [
    entry(10, "approval_decided", "approval.decided", { decision: "expired" }), // before any request: no proposal to join
    entry(30, "approval_decided", "approval.decided", { decision: "refused", reason: "approval request expired before a decision was made" }),
    entry(31, "approval_decided", "approval.decided", { decision: "expired" }),
    entry(47, "approval_decided", "approval.decided", { decision: "approved", actor: "console-user", approval_id: "apr-21dae2eb3e", proposal_id: LIVE_PROPOSAL, note: CONSOLE_NOTE }),
  ];
  const found = traceDecisions(requests, decisions);
  assert.deepEqual([...found.keys()], ["prop-a", LIVE_PROPOSAL]);
  assert.deepEqual(found.get("prop-a"), { proposalId: "prop-a", decision: "expired", actor: null, note: null, reason: null, seq: 31 });
  assert.deepEqual(found.get(LIVE_PROPOSAL), { proposalId: LIVE_PROPOSAL, decision: "approved", actor: "console-user", note: CONSOLE_NOTE, reason: null, seq: 47 });
  assert.ok(!JSON.stringify([...found.values()]).includes("apr-21dae2eb3e"), "the approval id is never read");
  assert.equal(traceDecisions(null, undefined).size, 0);
});

test("the trace's note stands in for a log entry that has none, for the same decision only", () => {
  const log = [approval(1, "p", "requested", "recap"), approval(2, "p", "approved", "console-user", "")];
  const traced = { proposalId: "p", decision: "approved", actor: "console-user", note: CONSOLE_NOTE, reason: null, seq: 47 };
  assert.equal(permissionLifecycle("p", log, false, traced).steps[1].note, CONSOLE_NOTE);
  assert.equal(permissionLifecycle("p", log, false, { ...traced, decision: "rejected" }).steps[1].note, null);
  const logged = [approval(1, "p", "requested", "recap"), approval(2, "p", "approved", "console-user", "interactive terminal; answered after 9.0 s")];
  assert.equal(permissionLifecycle("p", logged, false, traced).steps[1].note, "interactive terminal; answered after 9.0 s", "the log's own words win");
});

test("the evidence gate's rules: the failed one is read from RECAP's own problem text", () => {
  assert.deepEqual(PROPOSAL_RULES.map((rule) => rule.id), ["VP1-evidence-exists", "VP2-target-matches-diagnosis", "VP3-action-named", "VP4-restore-point"]);
  // run-e08c17d46e on SREGym's cluster: the model proposed the release the diagnosis blamed.
  assert.deepEqual(failedProposalRules(["the proposal restores 20260912.1, the release the diagnosis blames"]), ["VP4-restore-point"]);
  assert.deepEqual(failedProposalRules(["the proposal cites no evidence", "the proposal targets cart, but the diagnosis is inventory"]), [
    "VP1-evidence-exists",
    "VP2-target-matches-diagnosis",
  ]);
  assert.deepEqual(failedProposalRules(["the proposal cites evidence not in this run: ev-x"]), ["VP1-evidence-exists"]);
  assert.deepEqual(failedProposalRules(["there is no supported diagnosis to act on"]), ["VP2-target-matches-diagnosis"]);
  assert.deepEqual(failedProposalRules(["the proposal names no action"]), ["VP3-action-named"]);
  assert.deepEqual(failedProposalRules(["no deployment history or rollback dry run in this run names a version of geo to restore"]), ["VP4-restore-point"]);
  // Model text inside a problem cannot pose as another rule: only how RECAP's sentence starts counts.
  assert.deepEqual(failedProposalRules([`the fix targets cart ${HOSTILE} the proposal names no action`]), []);
});

test("the gate's rule ids and problem openings are the verifier's own (recap/agents/verifier.py)", () => {
  const verifier = readFileSync(`${REPO}recap/agents/verifier.py`, "utf8");
  for (const rule of PROPOSAL_RULES) {
    assert.ok(verifier.includes(`"${rule.id}"`), `${rule.id} is in PROPOSAL_RULES`);
    for (const start of rule.problems) assert.ok(verifier.includes(start), `the verifier writes "${start}…"`);
  }
});

test("a refused proposal names the rule it failed; unknown problem text keeps every rule the check ran", () => {
  const every = PROPOSAL_RULES.map((rule) => rule.id);
  const checks: ProposalCheckView[] = [
    { seq: 12, proposal_id: "prop-e08c", ok: false, problems: ["the proposal restores 20260912.1, the release the diagnosis blames"], rule_ids: every, enforced: true },
    { seq: 13, proposal_id: "prop-odd", ok: false, problems: ["a problem this dashboard cannot read"], rule_ids: every, enforced: true },
  ];
  const rows = blockedActions({ actions: [], gateway: [], checks, proposals: [], lifecycles: [] });
  assert.deepEqual(rows.map((r) => r.rule), ["VP4-restore-point", every.join(", ")]);
  const outcomes = runLayerOutcomes({ gateway: [], checks: checks.slice(0, 1), lifecycles: [], writes: [], counts: unsafeCounts(null), live: false });
  assert.deepEqual(outcomes[1].lines, ["failed VP4-restore-point", "the proposal restores 20260912.1, the release the diagnosis blames"]);
});

test("a fix the evidence gate refused is shown once, by the gate, though the run record lists it as denied", () => {
  // recap/harness/controller.py records a gate refusal as a "denied" action, like a policy DENY.
  const problem = "the proposal restores 20260912.1, the release the diagnosis blames";
  const proposals = [{ proposal_id: "prop-e08c", action: "execute_rollback", target: "geo", params: { to_version: "20260912.1" } } as unknown as ProposalView];
  const checks: ProposalCheckView[] = [{ seq: 44, proposal_id: "prop-e08c", ok: false, problems: [problem], rule_ids: PROPOSAL_RULES.map((r) => r.id), enforced: true }];
  const actions: ActionRecord[] = [{ action: "execute_rollback", target: "geo", params: { to_version: "20260912.1" }, result: "denied", at: "2026-11-03T20:10:00Z", note: problem }];
  const gateway = gatewayRows([policy(43, "execute_rollback", "APPROVAL_REQUIRED", ["R11-reversible-approval"], "controller", "reversible_write", ["needs an approval"])]);
  const rows = blockedActions({ actions, gateway, checks, proposals, lifecycles: [] });
  assert.deepEqual(rows.map((r) => [r.layer, r.what, r.result, r.rule, r.at]), [
    ["evidence_gate", "execute_rollback on geo (to_version=20260912.1)", "Refused", "VP4-restore-point", "2026-11-03T20:10:00Z"],
  ]);
  // A policy DENY on that tool, with no gate refusal: still the policy's own row, with its rule.
  const denyRollback = gatewayRows([policy(45, "execute_rollback", "DENY", ["R9-destructive-deny"], "controller", "destructive", ["no"])]);
  const denied = blockedActions({ actions, gateway: denyRollback, checks: [], proposals, lifecycles: [] });
  assert.deepEqual(denied.map((r) => [r.layer, r.result, r.rule]), [["policy", "Denied", "R9-destructive-deny"]]);
});

test("each write with the target's own answer: what it changed, or why it refused; never a capability or an approval id", () => {
  const sideEffects = ["geo generation 2 -> 3", "geo pod template image: ghcr.io/sregym/hotel-reservation:sha-d2c036bc3d1138f5a0bdaffd3d87f37522c3461a"];
  const writes = [
    entry(51, "write_executed", "mitigation.executed", {
      tool: "execute_rollback",
      target: "geo",
      params: { to_version: "sha-d2c036bc" },
      approval_id: "apr-21dae2eb3e",
      proposal_digest: DIGEST,
      capability: "cap.v1.SECRET",
      ok: true,
      error: null,
      side_effects: sideEffects,
    }),
    entry(60, "write_executed", "mitigation.executed", { tool: "execute_rollback", target: "geo", ok: false, error: "not authorized: the capability was already used" }),
  ];
  const rows = writeRows(writes);
  assert.deepEqual(rows[0], {
    seq: 51,
    ts: writes[0].ts,
    what: "execute_rollback on geo",
    params: "to_version=sha-d2c036bc",
    ok: true,
    effects: sideEffects,
    error: null,
    digest: `${DIGEST.slice(0, 12)}…`,
  });
  assert.deepEqual([rows[1].ok, rows[1].error, rows[1].effects, rows[1].digest], [false, "not authorized: the capability was already used", [], "Not recorded"]);
  for (const secret of ["apr-21dae2eb3e", "cap.v1.SECRET", DIGEST]) assert.ok(!JSON.stringify(rows).includes(secret), secret);

  // The write layer says what the target reported; a refusal is the layer stopping the fix.
  const [applied] = writes;
  const passed = runLayerOutcomes({ gateway: [], checks: [], lifecycles: [], writes: [applied], counts: unsafeCounts(null), live: false })[3];
  assert.deepEqual([passed.state, passed.headline, passed.lines], ["passed", "Signed write applied: execute_rollback on geo", sideEffects.map((e) => `the target reported: ${e}`)]);
  const refused = runLayerOutcomes({ gateway: [], checks: [], lifecycles: [], writes, counts: unsafeCounts(null), live: false })[3];
  assert.deepEqual([refused.state, refused.headline, refused.lines[0]], ["stopped", "Write failed or refused: execute_rollback on geo", "not authorized: the capability was already used"]);
});

test("a run's target in a sentence, from its recorded environment: never guessed, never the shop for a cluster", () => {
  assert.deepEqual(targetWords({ kind: "simulated", is_simulated: true }), { simulated: true, adapter: "simulated", short: "the shop", long: "the simulated shop" });
  assert.deepEqual(targetWords({ kind: "kind", is_simulated: false }), { simulated: false, adapter: "kubernetes", short: "the cluster", long: "the Kind demo cluster (live)" });
  assert.deepEqual(targetWords({ kind: "sregym", is_simulated: false }), { simulated: false, adapter: "kubernetes", short: "the cluster", long: "SREGym's cluster (live)" });
  assert.deepEqual(targetWords({ kind: "eks", is_simulated: false }), { simulated: false, adapter: null, short: "the target", long: "the eks target (live)" });
  assert.equal(targetWords({ kind: "", is_simulated: false }).long, "a live target (its kind not recorded)");
  assert.deepEqual(targetWords(null), { simulated: null, adapter: null, short: "the target", long: "the target (not recorded)" });
  assert.deepEqual(targetWords({ kind: "kind" }), targetWords(null), "no is_simulated: not recorded");
});

test("what each target's adapter checks: the simulator's capability, the Kubernetes adapter's dry run and read-back", () => {
  assert.match(adapterChecks(targetWords({ kind: "simulated", is_simulated: true })) ?? "", /single-use capability, signed for exactly that action/);
  const kube = adapterChecks(targetWords({ kind: "sregym", is_simulated: false })) ?? "";
  for (const part of ["only rollbacks and restarts", "nonce is spent before the cluster is touched", "server-side dry run as its RBAC-limited writer identity", "reads the Deployment back"]) {
    assert.ok(kube.includes(part), part);
  }
  assert.equal(adapterChecks(targetWords({ kind: "eks", is_simulated: false })), null, "an adapter this page does not know: nothing claimed");
  assert.equal(adapterChecks(targetWords(null)), null);
  // The layers' own text no longer assumes the simulator.
  assert.match(SAFETY_LAYERS[3].plain, /^The target applies a write only with a single-use capability/);
  assert.doesNotMatch(SAFETY_LAYERS[3].plain, /simulated shop/);
});

test("every test the page lists exists in this repository, the M5 live-target guards included", () => {
  for (const item of SAFETY_TESTS) {
    const source = readFileSync(`${REPO}${item.file}`, "utf8");
    assert.match(source, new RegExp(`def ${item.test}\\(`), `${item.file}::${item.test}`);
  }
  const listed = SAFETY_TESTS.map((item) => `${item.file}::${item.test}`);
  for (const wanted of [
    "tests/e2e/test_api_security.py::test_a_live_approval_needs_the_target_services_exact_name",
    "tests/unit/test_console_approval_live.py::test_a_prompt_nobody_answers_gives_up_when_the_request_expires",
    "tests/e2e/test_kind_pipeline.py::test_the_write_boundary_holds_inside_the_mcp_server_for_any_caller",
    "tests/unit/test_kind_engine.py::test_delete_namespace_is_refused_even_with_a_valid_capability",
  ]) {
    assert.ok(listed.includes(wanted), wanted);
  }
  assert.ok(SAFETY_TESTS.filter((item) => item.liveTarget).every((item) => !item.file.includes("test_kind_live")), "never a test that needs the real cluster (not in CI)");
  assert.equal(new Set(listed).size, listed.length, "no test listed twice");
});
