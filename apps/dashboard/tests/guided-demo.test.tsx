/**
 * Component tests for the Guided Demo (src/pages/GuidedDemo.tsx), rendered on the server: what
 * the start screen and a demo run show for given data. The run is a real replay of the golden
 * qwen3:8b recording (tests/fixtures, see tests/guided-demo.test.ts), cut where a visitor would
 * be: mid-investigation, waiting for the approval, approved and resolved, or rejected.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../src/api";
import { setToken } from "../src/auth";
import type * as dto from "../src/dto";
import { DemoRunView, DemoStartView, DemoStepper, previousDemoRuns } from "../src/pages/GuidedDemo";
import type { DemoRunViewProps, DemoStartViewProps } from "../src/pages/GuidedDemo";
import type { ApprovalRequest, EnvironmentInfo, ProviderInfo, TraceEvent } from "../src/types";
import { DEMO_PACES, demoProgress } from "../src/view/demo";
import approvedTrace from "./fixtures/golden-replay-approved.json";
import rejectedTrace from "./fixtures/golden-replay-rejected.json";
import { count, polled, render, tags, textOf, withAttr } from "./support/render";

const APPROVED = approvedTrace as unknown as TraceEvent[];
const REJECTED = rejectedTrace as unknown as TraceEvent[];
const TOKEN = "tok-NEVER-RENDER-7c1d";
const HOSTILE = `<script>alert(1)</script><img src=x onerror="alert(2)">`;
const noop = () => undefined;

function signedIn<T>(body: () => T): T {
  setToken(TOKEN);
  try {
    return body();
  } finally {
    setToken(null);
  }
}

function until(events: TraceEvent[], match: (e: TraceEvent) => boolean): TraceEvent[] {
  return events.slice(0, events.findIndex(match) + 1);
}

const FIRST = APPROVED[0].payload as { provider: Partial<ProviderInfo>; environment: Partial<EnvironmentInfo> };

function fixture(over: Partial<dto.FixtureView> = {}): dto.FixtureView {
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
    exported_run: null,
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
    ...over,
  } as dto.FixtureView;
}

const REPLAYS = { current_prompt_version: "prompts-v4.7", fixtures: [fixture()], manifest_error: null, how_to_launch: "" } as unknown as dto.ReplaysView;

function start(over: Partial<DemoStartViewProps> = {}): string {
  const props: DemoStartViewProps = {
    replays: polled(REPLAYS),
    environment: FIRST.environment,
    signedIn: false,
    pace: DEMO_PACES[0],
    onPace: noop,
    interview: false,
    onInterview: noop,
    starting: false,
    startError: null,
    onStart: noop,
    previous: [],
    now: Date.parse("2026-11-08T00:00:00Z"),
    ...over,
  };
  return render(<DemoStartView {...props} />);
}

function request(events: TraceEvent[]): ApprovalRequest {
  const asked = events.find((e) => e.category === "approval.requested");
  const proposed = events.find((e) => e.category === "mitigation.proposed");
  assert.ok(asked && proposed);
  const p = asked.payload as { proposal_id: string; digest: string; expires_at: string };
  return {
    proposal_id: p.proposal_id,
    run_id: asked.run_id,
    incident_id: "inc-1",
    proposal_digest: p.digest,
    proposal: { ...(proposed.payload as { proposal: ApprovalRequest["proposal"] }).proposal, proposal_id: p.proposal_id },
    status: "pending",
    requested_at: asked.ts,
    expires_at: p.expires_at,
  } as ApprovalRequest;
}

function run(events: TraceEvent[], over: Partial<DemoRunViewProps> = {}): string {
  const finished = events.some((e) => e.category === "run.summary");
  const props: DemoRunViewProps = {
    runId: events[0]?.run_id ?? "run-queued",
    events,
    finished,
    live: !finished,
    transport: finished ? "recorded" : "sse",
    streamError: null,
    interview: false,
    signedIn: false,
    provider: FIRST.provider,
    environment: FIRST.environment,
    summary: null,
    record: null,
    detailLoading: false,
    waiting: false,
    request: null,
    reviewable: false,
    onReview: noop,
    pinned: null,
    onPin: noop,
    technical: false,
    onTechnical: noop,
    onInterview: noop,
    onRestart: noop,
    restarting: false,
    restartError: null,
    ...over,
  };
  return render(<DemoRunView {...props} />);
}

// --- the start screen ------------------------------------------------------------------------------

test("the start screen: the provenance label word for word, the recording, and the ten steps ahead", () => {
  const html = start();
  const text = textOf(html);
  assert.equal(count(text, "Recorded real-model execution — replayed locally"), 1);
  assert.match(text, /The AI decisions are the answers qwen3:8b \(served by ollama\) gave in a real run on 2026-10-25, played back in order: no model is called now\./);
  assert.match(text, /Nothing real is changed\./);
  assert.match(text, /Unfamiliar incident, two specialists, owner-approved fix/);
  assert.match(text, /Recording intact/);
  assert.match(text, /Originally approved by the owner, at an interactive terminal \(162\.5 s after the request\)\. This replay asks you again\./);
  assert.equal(count(html, 'class="demo-preview"'), 1);
  assert.match(text, /An incident occurs .* Runbooks and memory are searched .* You approve or reject the fix .* Outcome and full trace/);
  assert.match(text, /It stops at step 7 and waits for your decision\./);
});

test("signed out, starting is disabled and the screen says why; signed in, it is one click", () => {
  const out = start();
  const button = tags(out, "button").find((b) => b.attrs.class?.includes("btn-large"));
  assert.ok(button && "disabled" in button.attrs);
  assert.match(textOf(out), /Sign in to start the demo and approve its fix\./);
  const html = start({ signedIn: true });
  const ready = tags(html, "button").find((b) => b.attrs.class?.includes("btn-large"));
  assert.ok(ready && !("disabled" in ready.attrs));
  assert.doesNotMatch(textOf(html), /Sign in to start the demo/);
});

test("paces and interview mode are offered; only the waiting changes", () => {
  const text = textOf(start());
  assert.match(text, /Guided Quick Instant/);
  assert.match(text, /reaches your approval in about two minutes\. Only the waiting changes, never what is replayed\./);
  assert.match(text, /Interview mode a presenter note at each step/);
});

test("on a live target replays are refused, so the demo cannot start there", () => {
  const html = start({ signedIn: true, environment: { kind: "kind", name: "Kind cluster recap-m5", is_simulated: false } });
  const button = tags(html, "button").find((b) => b.attrs.class?.includes("btn-large"));
  assert.ok(button && "disabled" in button.attrs);
  assert.match(textOf(html), /Replays are refused on this server's target/);
});

test("no launchable recording: the server's reasons, and no start", () => {
  const blocked = { ...REPLAYS, fixtures: [fixture({ launchable: false, not_launchable_because: ["its bytes do not match the SHA-256 the manifest pins"] })] };
  const html = start({ signedIn: true, replays: polled(blocked as dto.ReplaysView) });
  assert.match(textOf(html), /No recording can be replayed here its bytes do not match the SHA-256 the manifest pins/);
  const button = tags(html, "button").find((b) => b.attrs.class?.includes("btn-large"));
  assert.ok(button && "disabled" in button.attrs);
});

test("offline: the start screen says the recordings could not be loaded, and cannot start", () => {
  const offline = new ApiError("Failed to fetch", 0, "/replays", null, true);
  const html = start({ signedIn: true, replays: polled<dto.ReplaysView>(undefined, offline) });
  const text = textOf(html);
  assert.match(text, /Could not load the recordings \(GET \/replays\)/);
  const button = tags(html, "button").find((b) => b.attrs.class?.includes("btn-large"));
  assert.ok(button && "disabled" in button.attrs);
  // The label still says what a demo would be; it never claims a run happened.
  assert.match(text, /Recorded real-model execution — replayed locally/);
  assert.doesNotMatch(text, /Resolved|Approved by/);
});

test("earlier demo runs can be reviewed without signing in, in interview mode too", () => {
  const runs = [
    { run_id: "run-new", outcome: "resolved", created_at: "2026-11-07T23:50:00Z", replay: { fixture_id: "qwen3-8b-unfamiliar-parallel" } },
    { run_id: "run-other", outcome: "resolved", created_at: "2026-11-07T23:40:00Z", replay: { fixture_id: "qwen3-8b-bad-deployment" } },
    { run_id: "run-scripted", outcome: "failed", created_at: "2026-11-07T23:30:00Z", replay: null },
  ] as unknown as dto.RunSummary[];
  const previous = previousDemoRuns({ items: runs, total: 3 }, fixture());
  assert.deepEqual(
    previous.map((r) => r.run_id),
    ["run-new"],
  );
  const html = start({ previous, interview: true });
  assert.match(textOf(html), /Earlier demo runs on this server Reviewing a finished run needs no sign-in run-new Resolved 10 min ago/);
  assert.ok(tags(html, "a").some((a) => a.attrs.href === "#/demo?run=run-new&amp;mode=interview"));
});

// --- a demo run ------------------------------------------------------------------------------------

test("mid-investigation: the current step, the agents at work, and replayed model activity", () => {
  const events = until(APPROVED, (e) => e.category === "tool.call" && e.seq > 20);
  const html = run(events);
  const text = textOf(html);
  assert.match(text, /Recorded real-model execution — replayed locally/);
  assert.match(text, /Step 4 of 10 ?: AI agents inspect the evidence/);
  assert.match(text, /Working now Telemetry investigator AI agent · recorded answers .* Infrastructure investigator AI agent · recorded answers/);
  assert.match(text, /Model · replayed Recorded answer of qwen3:8b \(replayed, not called now\)/);
  // The status line is the one polite live region.
  assert.equal(withAttr(html, "aria-live", "polite").length, 1);
});

test("the stepper: ten buttons, the current step marked for assistive technology", () => {
  const progress = demoProgress(until(APPROVED, (e) => e.category === "approval.requested"));
  const html = render(<DemoStepper steps={progress.steps} shown="approve" onSelect={noop} />);
  const buttons = tags(html, "button");
  assert.equal(buttons.length, 10);
  assert.ok(buttons.every((b) => b.attrs.type === "button"));
  assert.equal(withAttr(html, "aria-pressed", "true").length, 1);
  const current = withAttr(html, "aria-current", "step");
  assert.equal(current.length, 1);
  assert.match(textOf(html), /Step 7: You approve or reject the fix Waiting for you/);
  assert.match(textOf(html), /Step 8: The approved fix runs Not yet/);
});

test("waiting for the approval: what happens either way, and the dialog behind one button", () => {
  const events = until(APPROVED, (e) => e.category === "approval.requested");
  const asked = request(events);
  const out = run(events, { waiting: true, request: asked });
  const text = textOf(out);
  assert.match(text, /Your decision is needed The run is paused\. Nothing has changed yet\. Restart the inventory service/);
  assert.match(text, /If you approve RECAP runs restart_service on inventory once, through the safety gateway, against RECAP's simulated shop\. Nothing real changes\./);
  assert.match(text, /If you reject Nothing runs\. The run ends as blocked, and your decision is logged\./);
  assert.match(text, /Why it asks you A reversible change/);
  assert.match(text, /Sign in to approve or reject this fix\./);
  assert.equal(count(text, "Sign in to approve or reject"), 1, "one sign-in prompt, in the decision card");
  const html = signedIn(() => run(events, { waiting: true, request: asked, signedIn: true, reviewable: true }));
  assert.ok(tags(html, "button").some((b) => textOf(html).includes("Review the fix and decide") && b.attrs.class?.includes("btn-primary")));
  assert.ok(!html.includes(TOKEN), "the operator token is never in the page");
});

test("approved and resolved: every step done, the outcome, and the way into the details", () => {
  const html = run(APPROVED);
  const text = textOf(html);
  // Before GET /runs/{id} answers, the status line reads the final state from the trace.
  assert.match(text, /Nothing real is changed\. Resolved elapsed/);
  assert.equal(count(html, 'class="demo-step status-done'), 10);
  assert.match(text, /Step 10 of 10 Done .* Outcome and full trace/);
  assert.match(text, /RESOLVED: oracle verified recovery \(simulated environment\)\./);
  assert.match(text, /Go deeper Every step above is in this run's trace/);
  assert.ok(tags(html, "a").some((a) => a.attrs.href === `#/timeline?run=${APPROVED[0].run_id}`));
  assert.match(text, /RECAP refused 1 model answer and asked again/);
  // Not following live any more: no stop button, no one "working now".
  assert.doesNotMatch(text, /Stop run|Working now/);
});

test("rejected: the fix and the recovery check are shown as skipped, with the reason", () => {
  const html = run(REJECTED, { pinned: "act" });
  const text = textOf(html);
  assert.match(text, /Step 8 of 10 Skipped .* The approved fix runs/);
  assert.match(text, /This step did not happen Not run: the fix was rejected, so nothing changed\./);
  assert.match(text, /Back to the current step/);
});

test("a chosen step shows its facts, model text quoted and attributed, rule codes explained", () => {
  const html = run(APPROVED, { pinned: "propose" });
  const text = textOf(html);
  assert.match(text, /Proposed: restart the inventory service \(restart_service on inventory\)\./);
  assert.match(text, /Written by the proposing model \(recorded model output, replayed\):/);
  assert.ok(count(html, 'class="untrusted"') >= 1);
  assert.match(text, /The 4 rules behind this VP1-evidence-exists Evidence gate\. A fix must cite evidence that exists in this run\./);
  assert.match(text, /The rule behind this R11-reversible-approval Safety policy\. A reversible change needs a written proposal/);
});

test("interview mode adds a presenter note; technical details add the codes, the run and the source", () => {
  const plain = run(APPROVED);
  assert.doesNotMatch(textOf(plain), /Presenter note\./);
  assert.doesNotMatch(textOf(plain), /event \d+ · tool\.result/);
  const html = run(APPROVED, { interview: true, technical: true });
  const text = textOf(html);
  assert.match(text, /Presenter note\. Open Trace Explorer to show any step in full/);
  assert.match(text, /event \d+ · (tool\.result|llm\.call|policy\.decision)/);
  assert.match(text, /Technical details Where this run's decisions come from/);
  assert.match(text, /RECORDED REAL-LLM RUN · SIMULATED/);
  assert.match(text, new RegExp(`Run ${APPROVED[0].run_id} ?: 128 trace events`));
});

test("a queued replay (Redis backend) carries the replay label before its first event", () => {
  const job = {
    job_id: "job-1",
    status: "queued",
    run_id: "run-queued",
    runs: ["run-queued"],
    scenario_id: "m3-inventory-leak-behind-frontend",
    attempt: 0,
    max_attempts: 3,
    position: 1,
    worker: null,
    worker_alive: null,
    submitted_at: null,
    claimed_at: null,
    finished_at: null,
    category: null,
    detail: null,
    cancel_requested: false,
    provider: "replay",
    replay_fixture: "qwen3-8b-unfamiliar-parallel",
  } as const;
  const text = textOf(run([], { provider: null, environment: null, live: true, transport: "sse", job }));
  assert.match(text, /Recorded real-model execution — replayed locally/);
  assert.match(text, /Queued for a worker: it will replay the committed recording qwen3-8b-unfamiliar-parallel offline/);
  assert.doesNotMatch(text, /This run is not a replay/);
  // A queued run that replays nothing is not called a replay.
  const scripted = textOf(run([], { provider: null, environment: null, live: true, transport: "sse", job: { ...job, provider: null, replay_fixture: null } }));
  assert.match(scripted, /This run is not a replay/);
});

test("a run that is not a replay never carries the replay label", () => {
  // A scripted run: its first event names the scripted provider, and no model is ever called.
  const scripted = APPROVED.filter((e) => e.category !== "llm.call").map((e, i) =>
    i === 0 ? { ...e, payload: { ...e.payload, provider: { name: "deterministic", kind: "scripted", origin: "scripted" } } } : e,
  );
  const html = run(scripted, { provider: { name: "deterministic", kind: "scripted", origin: "scripted" } });
  const text = textOf(html);
  assert.doesNotMatch(text, /Recorded real-model execution|AI agent · recorded answers|Model · replayed/);
  assert.match(text, /This run is not a replay/);
});

test("model and tool text is escaped, never markup", () => {
  const hostile = APPROVED.map((e) =>
    e.category === "agent.finish" ? { ...e, payload: { ...e.payload, summary: HOSTILE } } : e,
  );
  const html = run(hostile, { pinned: "investigate" });
  assert.ok(!html.includes("<script>"));
  assert.ok(!html.includes("<img"));
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
});

test("a run this server does not have says so, with a way back", () => {
  const html = run([APPROVED[0]], { streamError: new ApiError("Not Found", 404, "/runs/run-x", null, false), events: [], runId: "run-x" });
  assert.match(textOf(html), /This run is not on this server It may belong to another data folder\. Start a new guided demo/);
});
