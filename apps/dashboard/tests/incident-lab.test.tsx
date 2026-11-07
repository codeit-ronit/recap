/**
 * Component tests for the Incident Lab (src/pages/LiveRun.tsx and the components it owns:
 * ApprovalCard / ApprovalModal, EventFeed, StateStepper). Rendered on the server, so they check
 * what each view shows for given data: labels, roles, aria attributes, escaping, and the
 * loading / empty / error states. The data is shaped exactly like the API's JSON (src/dto.ts,
 * src/types.ts): GET /replays and GET /providers from a seeded server, and a paced replay of the
 * golden qwen3:8b recording against the simulated shop, stopped at its approval request.
 *
 * The live-target fixtures are shaped like the published Kind run (evals/live/2026-11-03-qwen3-
 * 8b-kind-human-approved-prompts-v4.7: execute_rollback on backend in Kind cluster recap-m5,
 * namespace recap-demo) and the SREGym run (geo, namespace hotel-reservation). A server render
 * cannot type, so the typed-name gate is checked through approveGate and namesTarget (the pure
 * rules the components use) and the first render; the request a decision sends goes through
 * sendDecision with a stubbed fetch.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../src/api";
import { setToken } from "../src/auth";
import { ApprovalCard, ApprovalModal, ConfirmTargetField, DecisionResult, approveNeedsName, sendDecision } from "../src/components/ApprovalCard";
import type { CitedEvidence } from "../src/components/ApprovalCard";
import { EventFeed } from "../src/components/EventFeed";
import { StateStepper } from "../src/components/StateStepper";
import type * as dto from "../src/dto";
import { parseHash } from "../src/hooks";
import {
  BackendChoice,
  InspectLinks,
  LiveRunPage,
  ObservedSignals,
  OutcomeCard,
  ReplayLauncher,
  RunBannerView,
  SafetyExplainer,
  SignInPrompt,
  TargetToken,
} from "../src/pages/LiveRun";
import type { ApprovalRequest, EnvironmentInfo, RunRecord, SystemSummary, TraceEvent } from "../src/types";
import { recoverySignals, runBanner, targetFacts } from "../src/view/lab";
import { count, polled, render, tags, textOf, withAttr } from "./support/render";

const noop = () => undefined;
const HOSTILE = `<script>alert(1)</script><img src=x onerror="alert(2)"><a href="javascript:alert(3)">x</a>`;
const TOKEN = "tok-NEVER-RENDER-9f2c";

/** Run `body` signed in with a fake operator token; always signs out again. */
function signedIn<T>(body: () => T): T {
  setToken(TOKEN);
  try {
    return body();
  } finally {
    setToken(null);
  }
}

const ENVIRONMENT: EnvironmentInfo = {
  kind: "simulated",
  name: "RECAP simulated shop",
  namespace: "shop",
  is_simulated: true,
  supported_actions: ["rollback", "restart", "delete_namespace"],
  description: "Deterministic local simulation of a five-service shop (state in a per-run SQLite file). No real cluster is involved.",
  stabilization_timeout_seconds: 0,
  stabilization_poll_seconds: 2,
};

/** The Kind target as GET /system and the run's first event report it (configs/targets/kind-demo). */
const KIND: EnvironmentInfo = {
  kind: "kind",
  name: "Kind cluster recap-m5",
  namespace: "recap-demo",
  is_simulated: false,
  supported_actions: ["rollback", "restart"],
  description:
    "Local Kind cluster recap-m5 (context kind-recap-m5), not production: 2 demo services in namespace recap-demo, read through typed kubectl operations.",
  stabilization_timeout_seconds: 180,
  stabilization_poll_seconds: 5,
};

/** SREGym's cluster (configs/targets/sregym-hotel). */
const SREGYM: EnvironmentInfo = {
  ...KIND,
  kind: "sregym",
  name: "SREGym on Kind cluster kind",
  namespace: "hotel-reservation",
  description: "SREGym 1.1 on its own Kind cluster kind (context kind-kind), not production: 19 Deployments of a benchmark application.",
  stabilization_timeout_seconds: 240,
};

function fixture(over: Partial<dto.FixtureView> = {}): dto.FixtureView {
  return {
    id: "qwen3-8b-unfamiliar-parallel",
    title: "Unfamiliar incident, two specialists, owner-approved fix (M3 deliverable)",
    description:
      "qwen3:8b on the held-out m3-inventory-leak-behind-frontend incident: two specialists in parallel for two rounds; resource pressure on inventory supported; oracle verified.",
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

const SINGLE = fixture({
  id: "qwen3-8b-bad-deployment",
  title: "Bad deployment, one investigator (CI fixture)",
  description: "qwen3:8b on bad-deployment: one investigator, six reads, a rollback, oracle verified.",
  scenario: "bad-deployment",
  strategy: "adaptive",
  use_memory: false,
  agents: "one investigator",
  approved_by: "the simulation auto-approver",
  exported_run: null,
  tags: ["single-agent"],
  recording_id: "20261025-110603-ollama-b1ae62",
  bytes: 95811,
  sha256: "991c1192dd6d",
});

function replays(fixtures: dto.FixtureView[], manifest_error: string | null = null): dto.ReplaysView {
  return { schema_version: "recap-api-v1", current_prompt_version: "prompts-v4.7", fixtures, manifest_error, how_to_launch: "POST /incidents/run (operator) …" };
}

function launcher(over: Partial<Parameters<typeof ReplayLauncher>[0]> = {}) {
  return render(
    <ReplayLauncher
      replays={polled(replays([fixture(), SINGLE]))}
      environment={ENVIRONMENT}
      signedIn
      pace={0.05}
      onPace={noop}
      starting={null}
      startError={null}
      onReplay={noop}
      {...over}
    />,
  );
}

const LIMITS: dto.ProviderLimitsView = {
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
    limits: LIMITS,
    limitations: [],
    ...over,
  };
}

const PROVIDERS: dto.ProviderView[] = [
  provider({
    name: "deterministic",
    kind: "scripted",
    origin: "scripted",
    default: true,
    selectable: true,
    description: "Scripted baseline policy (not an LLM): deterministic playbooks and heuristics.",
    detail: "scripted policy, not an LLM; $0",
    selection_rule: "the server's default provider",
    limitations: ["Not an LLM: fixed playbooks and heuristics written for the simulated incidents."],
  }),
  provider({
    name: "ollama",
    kind: "llm",
    origin: "local_llm",
    model: "qwen3:8b",
    mode: "tools",
    selectable: true,
    description: "Open-weight model served by Ollama on this machine. No network, no charge.",
    detail: "local model installed; $0",
    selection_rule: "marked api_selectable in configs/providers.yaml by the owner",
  }),
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
  provider({
    name: "anthropic",
    kind: "llm",
    origin: "paid_llm",
    status: "disabled",
    key_required: true,
    selection_rule: "a paid API: never selectable from the API (this project's budget is $0)",
  }),
];

const DIGEST = "99ea7f82a85123ed663d2a94ef703840de868a23b7277eef97187a3cba2026b2";
const RATIONALE =
  "The verified root cause is resource_pressure on the inventory service, supported by the OOMKilled events and the evidence ev-26c5fb-r2i02.";

function request(over: Partial<ApprovalRequest> = {}, minutesLeft = 14): ApprovalRequest {
  return {
    proposal_id: "prop-0d046a159f",
    run_id: "run-26c5fb3c51",
    incident_id: "inc-e1f2206dfa",
    proposal_digest: DIGEST,
    proposal: {
      action: "restart_service",
      target: "inventory",
      params: {},
      rationale: RATIONALE,
      evidence_ids: ["ev-26c5fb-r2i02"],
      expected_health_change: "The restart should restore the inventory service's health.",
      rollback_hint: "If the restart does not help, consider rolling back inventory after approval.",
      risk_level: "reversible_write",
      hypothesis_claim: "Inventory's resource pressure and OOMKilled events.",
      source: "llm",
      citations: ["runbook-memory-pressure#remediation-1", "shop-architecture#services-1"],
      proposal_id: "prop-0d046a159f",
      incident_id: "inc-e1f2206dfa",
      created_at: "2026-10-26T05:09:14.138047Z",
    },
    status: "pending",
    requested_at: "2026-10-26T05:09:14.148202Z",
    expires_at: new Date(Date.now() + minutesLeft * 60_000).toISOString(),
    ...over,
  };
}

const EVIDENCE: dto.EvidenceItem = {
  id: "ev-26c5fb-r2i02",
  source_type: "tool",
  source_ref: "mcp:get_k8s_like_events(limit=5,service=inventory)",
  tool: "get_k8s_like_events",
  timestamp: "2026-10-26T05:09:05.162598Z",
  recorded_by: "infrastructure_investigator",
  round: 2,
  trust_class: "untrusted_tool_output",
  trust_level: "untrusted",
  flags: [],
  token_estimate: 275,
  content: '{"events":[{"reason":"OOMKilled","service":"inventory","message":"Container inventory exceeded its memory limit (768Mi) and was killed"}]}',
  supports: [],
  contradicts: [],
  in_rule_basis: true,
};

const POLICY: dto.PolicyVerdictView = {
  tool: "restart_service",
  verdict: "APPROVAL_REQUIRED",
  rule_ids: ["R1-tool-name-format", "R2-known-tool", "R7-worker-read-only", "R14-remediation-catalog", "R11-reversible-approval"],
  reasons: ["reversible write: needs a human approval bound to this exact proposal"],
  risk: "reversible_write",
  caller: "controller",
  decided_at: "2026-10-26T05:09:14.140000Z",
};

const CHECK: dto.ProposalCheckView = {
  seq: 116,
  proposal_id: "prop-0d046a159f",
  ok: true,
  problems: [],
  rule_ids: ["VP1-evidence-exists", "VP2-target-matches-diagnosis", "VP3-action-named", "VP4-restore-point"],
  enforced: true,
};

const REPLAYED = { who: "the recorded model (qwen3:8b), replayed", trusted: false };

/** The approval dialog, on the simulated shop unless a test names another target. */
function modal(over: Partial<Parameters<typeof ApprovalModal>[0]> = {}) {
  const evidence: CitedEvidence = { registry: [EVIDENCE] };
  return render(
    <ApprovalModal
      request={request()}
      open
      onDefer={noop}
      trustedRisk="reversible_write"
      policy={POLICY}
      check={CHECK}
      author={REPLAYED}
      evidence={evidence}
      environment={ENVIRONMENT}
      {...over}
    />,
  );
}

/** The live Kind run's pending request: roll backend back to v1.0.0 (prop-9f461dcb40). */
function liveRequest(over: Partial<ApprovalRequest> = {}, minutesLeft = 14): ApprovalRequest {
  const base = request();
  return request(
    {
      proposal_id: "prop-9f461dcb40",
      run_id: "run-047aac6eb0",
      incident_id: "inc-9d99ba533a",
      proposal_digest: "917e98b72d1e4684f6aa77928897a945f90517f991a244aecdb3ae9f9e9c5239",
      proposal: {
        ...base.proposal,
        action: "execute_rollback",
        target: "backend",
        params: { to_version: "v1.0.0" },
        rationale: "The verified root cause is a bad deployment of the backend service. Roll back to the previous healthy version.",
        evidence_ids: ["ev-047aac-r1i04", "ev-047aac-r1i06"],
        proposal_id: "prop-9f461dcb40",
        incident_id: "inc-9d99ba533a",
      },
      ...over,
    },
    minutesLeft,
  );
}

function buttonNamed(html: string, name: RegExp): { disabled: boolean } | null {
  for (const match of html.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)) {
    if (name.test(textOf(match[2]))) return { disabled: /\sdisabled=""/.test(match[1]) };
  }
  return null;
}

let seq = 1;
function event(category: string, payload: Record<string, unknown>, ms = 0, agent: string | null = null): TraceEvent {
  const n = seq++;
  return {
    event_id: `evt-${n}`,
    run_id: "run-26c5fb3c51",
    seq: n,
    stage: "INVESTIGATING",
    ts: new Date(Date.parse("2026-10-26T05:08:47.260Z") + ms).toISOString(),
    agent,
    category,
    payload,
    duration_ms: null,
    usage: null,
    schema_version: "recap.v1",
  };
}

const REPLAY_PROVIDER = {
  name: "replay:ollama:qwen3:8b",
  kind: "replay" as const,
  origin: "replay" as const,
  model: "qwen3:8b",
  replay_of: { recording_id: "20261025-121828-ollama-6a5cfd", original_provider: "ollama", model: "qwen3:8b" },
};

function summary(over: Partial<dto.RunSummary> = {}): dto.RunSummary {
  return {
    run_id: "run-26c5fb3c51",
    scenario_id: "m3-inventory-leak-behind-frontend",
    state: "RESOLVED",
    label: "",
    created_at: "2026-10-26T05:08:47.260809Z",
    updated_at: "2026-10-26T05:09:41.800000Z",
    modes: ["parallel", "parallel"],
    recovered: true,
    outcome_note: "oracle verified recovery (simulated environment)",
    provider: "replay:ollama:qwen3:8b",
    tool_calls: 17,
    pending_approval: false,
    status: "finished",
    terminal: true,
    outcome: "resolved",
    provider_class: "replay",
    provider_kind: "replay",
    model: "qwen3:8b",
    prompt_version: "prompts-v4.7",
    environment_kind: "simulated",
    environment_simulated: true,
    config_hash: "394843bc06d11ea0",
    code_version: "cdc5df8a7f2f",
    recap_version: "0.4.0",
    started_at: "2026-10-26T05:08:47.260809Z",
    ended_at: "2026-10-26T05:09:41.714000Z",
    wall_seconds: 54.453,
    rounds: 2,
    agents: ["telemetry_investigator", "infrastructure_investigator"],
    model_requests: 0,
    oracle: {
      oracle: "simulated-health-oracle",
      verified: true,
      healthy_before: false,
      healthy_after: true,
      checked_at: "2026-03-18T19:45:00Z",
      services_checked: ["cart", "checkout", "frontend", "inventory", "payment"],
    },
    replay: {
      fixture_id: "qwen3-8b-unfamiliar-parallel",
      recording_id: "20261025-121828-ollama-6a5cfd",
      recording_matches_fixture: true,
      committed_fixture: true,
      original_provider: "ollama",
      model: "qwen3:8b",
      prompt_version: "prompts-v4.7",
      recorded_at: "2026-10-25T12:18:28.811038+00:00",
      fixture_title: "Unfamiliar incident, two specialists, owner-approved fix (M3 deliverable)",
      fixture_sha256: "37830779420b",
      original_export: "evals/live/2026-10-25-qwen3-8b-human-approved-unfamiliar-prompts-v4.7/",
      originally_approved_by: "the owner, at an interactive terminal (162.5 s after the request)",
    },
    export: null,
    recording: null,
    episode_id: null,
    error: null,
    labels: ["SIMULATED ENVIRONMENT", "REPLAY", "RECORDED REAL-LLM RUN"],
    not_recorded: [],
    ...over,
  };
}

/** The parts of the run record the outcome reads (the rest of a RunRecord is not shown there). */
function recordWith(approvals: RunRecord["approvals"]): RunRecord {
  return {
    run_id: "run-26c5fb3c51",
    scenario_id: "m3-inventory-leak-behind-frontend",
    approvals,
    environment: ENVIRONMENT,
    provider: "replay:ollama:qwen3:8b",
  } as RunRecord;
}

const APPROVED: RunRecord["approvals"][number] = {
  approval_id: "apr-a15df3cc84",
  incident_id: "inc-e1f2206dfa",
  proposal_id: "prop-0d046a159f",
  proposal_digest: DIGEST,
  scope: "single_action",
  decision: "approved",
  actor: "operator:local-operator",
  decided_at: "2026-10-26T05:09:41.288504Z",
  expires_at: "2026-10-26T05:24:14.148202Z",
  consumed_at: "2026-10-26T05:09:41.300000Z",
};

// ===============================================================================================
// Replay launcher
// ===============================================================================================

test("the replay launcher lists each recording: model, prompts, agents, original approval, integrity", () => {
  const html = launcher();
  const text = textOf(html);
  assert.match(text, /Replay a recorded incident/);
  assert.match(text, /Unfamiliar incident, two specialists, owner-approved fix/);
  assert.match(text, /Bad deployment, one investigator \(CI fixture\)/);
  assert.equal(count(text, "REPLAY · RECORDED REAL-LLM RUN"), 2);
  assert.equal(count(text, "SIMULATED TARGET"), 2);
  assert.match(text, /qwen3:8b via ollama/);
  assert.match(text, /qwen3:8b via ollama prompts-v4\.7 prompts match this server/);
  assert.match(text, /Replay this incident Recording intact Scenario/); // the action comes right after the description
  assert.match(text, /two specialists in parallel \(telemetry, infrastructure\), two rounds/);
  assert.match(text, /parallel routing · memory on · bm25 search/);
  assert.match(text, /the owner, at an interactive terminal \(162\.5 s after the request\)/);
  assert.match(text, /The replay asks a person again: nothing is approved for you\./);
  assert.equal(count(text, "Intact"), 2);
  assert.match(text, /sha256 37830779420b/);
  assert.match(text, /225,578 bytes/);
  // Each card is an article named by its title.
  const articles = tags(html, "article");
  assert.equal(articles.length, 2);
  for (const article of articles) assert.ok(withAttr(html, "id", article.attrs["aria-labelledby"]).length === 1);
});

test("the pace choice: watchable by default, instant on request, explained in one sentence", () => {
  const html = launcher();
  const group = withAttr(html, "role", "radiogroup").find((t) => t.attrs["aria-label"] === "Replay pace");
  assert.ok(group);
  const radios = withAttr(html, "role", "radio");
  assert.deepEqual(
    radios.map((r) => r.attrs["aria-checked"]),
    ["true", "false"],
  );
  assert.match(textOf(html), /Watchable \(about half a minute\)/);
  assert.match(textOf(html), /5% of the time the model originally took/);
  const instant = launcher({ pace: 0 });
  assert.deepEqual(
    withAttr(instant, "role", "radio").map((r) => r.attrs["aria-checked"]),
    ["false", "true"],
  );
  assert.match(textOf(instant), /reaches your approval within seconds/);
});

test("signed in, each replay starts with one click; signed out, the buttons are disabled and say why", () => {
  const html = launcher();
  const on = [...html.matchAll(/<button([^>]*)>(?:(?!<\/button>)[\s\S])*Replay this incident/g)];
  assert.equal(on.length, 2);
  for (const match of on) assert.doesNotMatch(match[1], /disabled/);
  const off = launcher({ signedIn: false });
  assert.equal(buttonNamed(off, /Replay this incident/)?.disabled, true);
  assert.equal(count(textOf(off), "Sign in to start it."), 2);
});

test("while one replay starts, its button says so and no second replay can start", () => {
  const html = launcher({ starting: "qwen3-8b-bad-deployment" });
  assert.match(textOf(html), /Starting the replay…/);
  assert.equal(buttonNamed(html, /Replay this incident/)?.disabled, true);
});

test("a refused start shows the server's message on that recording's card", () => {
  const error = new ApiError("replay fixture 'qwen3-8b-bad-deployment' cannot run: its file is missing", 400, "/incidents/run", null, false);
  const html = launcher({ startError: { fixtureId: "qwen3-8b-bad-deployment", error } });
  assert.match(textOf(html), /The replay did not start · HTTP 400 replay fixture 'qwen3-8b-bad-deployment' cannot run: its file is missing/);
  assert.equal(withAttr(html, "role", "alert").length, 1);
});

test("a recording the server cannot replay shows its reasons (escaped) and no replay button", () => {
  const altered = fixture({
    integrity: "altered",
    launchable: false,
    not_launchable_because: ["its bytes do not match the SHA-256 the manifest pins (altered)", HOSTILE],
  });
  const html = launcher({ replays: polled(replays([altered])) });
  const text = textOf(html);
  assert.match(text, /Altered/);
  assert.match(text, /Cannot be replayed on this server/);
  assert.match(text, /its bytes do not match the SHA-256 the manifest pins \(altered\)/);
  assert.equal(buttonNamed(html, /Replay this incident/), null);
  assert.doesNotMatch(html, /<script|<img|href="javascript/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
});

test("a prompt-version mismatch and a missing scenario are stated, not hidden", () => {
  const html = launcher({
    replays: polled(replays([fixture({ prompt_version: "prompts-v4.6", prompt_version_matches: false, scenario_available: false, launchable: false, not_launchable_because: ["it was recorded with prompts-v4.6, this server renders prompts-v4.7: its requests would not match"] })])),
  });
  const text = textOf(html);
  assert.match(text, /this server renders prompts-v4\.7/);
  assert.match(text, /not on this server/);
});

test("the replay launcher's loading, empty and error states", () => {
  assert.match(textOf(launcher({ replays: polled<dto.ReplaysView>(undefined) })), /Loading the recordings…/);
  const empty = textOf(launcher({ replays: polled(replays([])) }));
  assert.match(empty, /No recordings to replay/);
  assert.match(empty, /lists no recordings/);
  assert.match(textOf(launcher({ replays: polled(replays([], "MANIFEST.json: invalid JSON")) })), /cannot be read: MANIFEST\.json: invalid JSON/);
  const failed = launcher({ replays: polled<dto.ReplaysView>(undefined, new ApiError("Internal Server Error", 500, "/replays", null, false)) });
  assert.match(textOf(failed), /Could not load the recordings \(GET \/replays\)/);
  assert.equal(buttonNamed(failed, /Retry/)?.disabled, false);
});

test("without the server's environment a replay card shows its source alone, never a guessed target", () => {
  const text = textOf(launcher({ environment: undefined }));
  assert.match(text, /REPLAY · RECORDED REAL-LLM RUN/);
  assert.doesNotMatch(text, /SIMULATED TARGET/);
});

// ===============================================================================================
// Backend choice, safety and sign-in
// ===============================================================================================

test("only backends the server allows per run are offered, the default preselected", () => {
  const html = render(<BackendChoice providers={polled(PROVIDERS)} value="deterministic" onChange={noop} />);
  const radios = tags(html, "input").filter((t) => t.attrs.type === "radio");
  assert.deepEqual(
    radios.map((r) => [r.attrs.value, "checked" in r.attrs]),
    [
      ["deterministic", true],
      ["ollama", false],
    ],
  );
  // Each radio is named by its label and described by what the backend is.
  for (const radio of radios) {
    assert.equal(tags(html, "label").filter((l) => l.attrs.for === radio.attrs.id).length, 1);
    assert.equal(withAttr(html, "id", radio.attrs["aria-describedby"]).length, 1);
  }
  const text = textOf(html);
  assert.match(text, /server default/);
  assert.match(text, /SCRIPTED BASELINE/);
  assert.match(text, /LOCAL LLM qwen3:8b/);
  assert.match(text, /Not an LLM: fixed playbooks/);
});

test("the other backends are listed with the server's reason they cannot be chosen", () => {
  const text = textOf(render(<BackendChoice providers={polled(PROVIDERS)} value="deterministic" onChange={noop} />));
  assert.match(text, /Not available for a run started here/);
  assert.match(text, /groq .*Needs setup chosen in server configuration only \(api_selectable is off for it\)/);
  assert.match(text, /anthropic .*PAID LLM.*a paid API: never selectable from the API/);
  assert.match(text, /replay needs a recording: start it from Replay a recorded incident above\./);
});

test("a chosen backend the server reports as not ready is flagged before the run starts", () => {
  const down = PROVIDERS.map((p) => (p.name === "ollama" ? { ...p, status: "unavailable", detail: "Ollama is not running at http://127.0.0.1:11434" } : p));
  const text = textOf(render(<BackendChoice providers={polled(down)} value="ollama" onChange={noop} />));
  assert.match(text, /ollama: unavailable/);
  assert.match(text, /Ollama is not running at http:\/\/127\.0\.0\.1:11434 A run started with it now would likely fail\./);
});

test("the backend choice's loading, empty and error states", () => {
  assert.match(textOf(render(<BackendChoice providers={polled<dto.ProviderView[]>(undefined)} value={null} onChange={noop} />)), /Loading the backends…/);
  assert.match(textOf(render(<BackendChoice providers={polled([])} value={null} onChange={noop} />)), /The server reports no providers/);
  const failed = render(<BackendChoice providers={polled<dto.ProviderView[]>(undefined, new ApiError("boom", 500, "/providers", null, false))} value={null} onChange={noop} />);
  assert.match(textOf(failed), /Could not load the backends \(GET \/providers\)/);
  assert.ok(buttonNamed(failed, /Retry/));
  const none = textOf(render(<BackendChoice providers={polled([PROVIDERS[2]])} value={null} onChange={noop} />));
  assert.match(none, /No backend can be chosen for a run here/);
});

test("the safety notes name the server's environment and hold for every run", () => {
  const text = textOf(render(<SafetyExplainer environment={ENVIRONMENT} />));
  assert.match(text, /Simulated target\. RECAP simulated shop: Deterministic local simulation of a five-service shop/);
  assert.match(text, /No real system is changed\./);
  assert.match(text, /Read-only agents\./);
  assert.match(text, /Exact approval\. A write runs only with an approval bound by digest/);
  assert.match(text, /Safety stays on\./);
  assert.match(textOf(render(<SafetyExplainer environment={{ ...ENVIRONMENT, is_simulated: false, kind: "kind" }} />)), /Live target \(kind\)\./);
  assert.match(textOf(render(<SafetyExplainer environment={undefined} />)), /Target not reported yet\./);
});

test("the sign-in prompt says what needs the token, where it is, and that watching does not", () => {
  const html = render(<SignInPrompt />);
  assert.equal(withAttr(html, "role", "note").length, 1);
  const text = textOf(html);
  assert.match(text, /Sign in to start runs and decide approvals\./);
  assert.match(text, /data\/operator-token/);
  assert.match(text, /Watching runs needs no sign-in\./);
  assert.ok(buttonNamed(html, /Open sign-in/));
});

// ===============================================================================================
// The run: banner, stepper, feed, outcome
// ===============================================================================================

test("a replayed run is labelled RECORDED REAL-LLM RUN · SIMULATED TARGET, with its recording", () => {
  const html = render(<RunBannerView banner={runBanner(REPLAY_PROVIDER, ENVIRONMENT)} replay={summary().replay} />);
  const text = textOf(html);
  assert.match(text, /^RECORDED REAL-LLM RUN · SIMULATED TARGET/);
  assert.match(text, /qwen3:8b via ollama gave in an earlier run are replayed offline: no model is called now\./);
  assert.match(text, /Recording 20261025-121828-ollama-6a5cfd of “Unfamiliar incident/);
  assert.match(text, /\(matches its committed fixture\)/);
  assert.match(text, /The original fix was approved by the owner, at an interactive terminal .* this replay asks again\./);
  const scripted = textOf(render(<RunBannerView banner={runBanner({ origin: "scripted", kind: "scripted" }, ENVIRONMENT)} />));
  assert.match(scripted, /^SCRIPTED BASELINE \(NOT AN LLM\) · SIMULATED TARGET/);
  assert.match(textOf(render(<RunBannerView banner={runBanner(null, null)} />)), /SOURCE NOT RECORDED · TARGET NOT RECORDED/);
});

test("the stepper marks the current stage, writes each status out, and shows when stages began", () => {
  const html = render(
    <StateStepper
      states={["CREATED", "RETRIEVING", "INVESTIGATING", "INVESTIGATING", "PROPOSING", "AWAITING_APPROVAL"]}
      running
      times={{ CREATED: 0, RETRIEVING: 1441, INVESTIGATING: 1442, PROPOSING: 23_490, AWAITING_APPROVAL: 26_887 }}
      caption="Awaiting approval. The safety policy needs a person to approve this exact action."
    />,
  );
  const current = withAttr(html, "aria-current", "step");
  assert.equal(current.length, 1);
  const text = textOf(html);
  assert.match(text, /Awaiting approval \(current\)/);
  assert.match(text, /Investigating ×2 \(done, 2 rounds\)/);
  assert.match(text, /Resolved \(not reached\)/);
  assert.match(text, /\+1\.4 s/);
  assert.match(text, /\+27 s/);
  assert.equal(withAttr(html, "role", "status").length, 1);
  assert.equal(tags(html, "ol")[0].attrs["aria-label"], "Run stages");
});

test("a blocked run shows where it stopped, in words", () => {
  const text = textOf(render(<StateStepper states={["CREATED", "RETRIEVING", "INVESTIGATING", "PROPOSING", "AWAITING_APPROVAL", "BLOCKED"]} running={false} />));
  assert.match(text, /Awaiting approval \(stopped here\)/);
  assert.match(text, /Mitigating \(skipped\)/);
  assert.match(text, /Ended in Blocked while awaiting approval/);
});

test("the feed is bounded: a long trace renders its newest page, with the rest one click away", () => {
  seq = 1;
  const events = Array.from({ length: 1200 }, (_, i) =>
    event("tool.call", { invocation_id: `call-${i}`, tool: "get_service_logs", args: { service: "inventory", limit: 20 }, caller: "telemetry_investigator" }, i * 10, "telemetry_investigator"),
  );
  const html = render(<EventFeed events={events} live height={560} />);
  assert.equal(count(html, 'class="feed-item '), 200);
  const text = textOf(html);
  assert.match(text, /1,200 events/);
  assert.match(text, /Show 200 earlier events 1,000 earlier not shown/);
  const log = withAttr(html, "role", "log");
  assert.equal(log.length, 1);
  assert.equal(log[0].attrs["aria-live"], "off");
  assert.equal(log[0].attrs["aria-label"], "Trace events");
  assert.equal(log[0].attrs.tabindex, "0");
  assert.match(text, /live/);
  // The newest line is the last one rendered.
  assert.match(html, /call-1199|\+11\.99s/);
});

test("the feed waits for its first event, hides routine internals by default, and escapes agent text", () => {
  assert.match(textOf(render(<EventFeed events={[]} live />)), /Waiting for the first trace event…/);
  seq = 1;
  const events = [
    event("context.pack", { step: 1, tokens_before: 900, tokens_after: 800, budget_tokens: 3000, dropped: [] }, 0, "telemetry_investigator"),
    event("tool.call", { tool: "search_runbooks", args: { query: HOSTILE }, caller: "telemetry_investigator" }, 120, "telemetry_investigator"),
  ];
  const html = render(<EventFeed events={events} />);
  assert.match(textOf(html), /2 events · 1 routine internals hidden/);
  assert.equal(count(html, 'class="feed-item '), 1);
  assert.doesNotMatch(html, /<script|<img|href="javascript/);
  assert.match(html, /<q class="feed-quote"[^>]*>&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
});

test("the outcome: resolved with the oracle's verdict, the approval, the data source and the run's pages", () => {
  const html = render(<OutcomeCard runId="run-26c5fb3c51" summary={summary()} record={recordWith([APPROVED])} />);
  const text = textOf(html);
  assert.match(text, /Outcome/);
  assert.match(text, /Resolved The independent oracle verified that the simulated shop recovered\./);
  assert.match(text, /RECAP's note: oracle verified recovery \(simulated environment\)/);
  assert.match(text, /Recovery verified unhealthy before → healthy after · simulated-health-oracle · checked cart, checkout, frontend, inventory, payment/);
  assert.match(text, /approved by operator:local-operator used once/);
  assert.match(text, /54\.5 s · from start to end, including any wait for a person/);
  assert.match(text, /REPLAY · RECORDED REAL-LLM RUN qwen3:8b \/ SIMULATED TARGET/);
  const links = tags(html, "a").map((a) => a.attrs.href);
  assert.deepEqual(links, [
    "#/timeline?run=run-26c5fb3c51",
    "#/evidence?run=run-26c5fb3c51",
    "#/knowledge?run=run-26c5fb3c51",
    "#/memory?run=run-26c5fb3c51",
    "#/safety?run=run-26c5fb3c51",
  ]);
  assert.match(text, /Trace Explorer.*Evidence & Decisions.*Knowledge.*Experience Memory.*Safety & Approvals/);
});

test("a rejected fix: blocked, the oracle not consulted, and why", () => {
  const rejected = { ...APPROVED, decision: "rejected" as const, consumed_at: null };
  const html = render(
    <OutcomeCard
      runId="run-ae3c6df71d"
      summary={summary({ run_id: "run-ae3c6df71d", state: "BLOCKED", outcome: "approval_rejected", recovered: false, oracle: null, outcome_note: "mitigation rejected by operator:local-operator" })}
      record={recordWith([rejected])}
    />,
  );
  const text = textOf(html);
  assert.match(text, /Blocked: fix rejected A person rejected the proposed fix, so it did not run\./);
  assert.match(text, /Not consulted No fix reached the oracle, so it was not asked\./);
  assert.match(text, /rejected by operator:local-operator not used/);
});

test("a partial record reads Not recorded; a missing summary waits or says so; hostile notes are escaped", () => {
  const partial = textOf(render(<OutcomeCard runId="run-x" summary={summary({ wall_seconds: null, outcome: "in_progress", replay: null, environment_simulated: null })} record={null} />));
  assert.match(partial, /In progress/);
  assert.match(partial, /Wall time Not recorded/);
  assert.match(partial, /Approval Not recorded/);
  assert.match(partial, /TARGET NOT RECORDED/);
  assert.match(textOf(render(<OutcomeCard runId="run-x" summary={null} record={null} loading />)), /Loading the final checkpoint…/);
  assert.match(textOf(render(<OutcomeCard runId="run-x" summary={null} record={null} />)), /No outcome recorded/);
  const hostile = render(<OutcomeCard runId="run-x" summary={summary({ outcome_note: HOSTILE, error: HOSTILE, outcome: "error" })} record={null} />);
  assert.doesNotMatch(hostile, /<script|<img|href="javascript/);
  assert.match(textOf(hostile), /Error/);
});

test("the inspect links name each page and what it shows", () => {
  const text = textOf(render(<InspectLinks runId="run-1" />));
  assert.match(text, /Inspect this run/);
  assert.match(text, /Trace Explorer Each agent's tool calls and model requests on one timeline/);
  assert.match(text, /Safety & Approvals Policy verdicts and the approval log/);
});

// ===============================================================================================
// The approval dialog
// ===============================================================================================

test("the approval dialog cannot be dismissed and opens on the exact action", () => {
  const html = signedIn(() => modal());
  const dialog = withAttr(html, "role", "dialog");
  assert.equal(dialog.length, 1);
  assert.equal(dialog[0].attrs["aria-modal"], "true");
  assert.equal(withAttr(html, "aria-label", "Close dialog").length, 0); // no close button: the decision is the point
  const text = textOf(html);
  assert.match(text, /^Approve this exact action\?/);
  assert.match(text, /digest 99ea7f82a85123ed… proposal prop-0d046a159f expires /);
  assert.match(text, /Nothing changes in the simulated shop until you decide/);
  const group = withAttr(html, "aria-label", "The proposed action");
  assert.equal(group.length, 1);
  assert.equal(group[0].attrs.tabindex, "-1");
  assert.match(text, /restart_service on inventory no params/);
  assert.match(text, /trusted risk: reversible_write/);
  assert.match(text, /declared: reversible_write/);
  assert.match(text, /1[34]:\d\d left/);
});

test("the digest shows in short form only; the token is never rendered", () => {
  const html = signedIn(() => modal());
  assert.match(textOf(html), /99ea7f82a85123ed…/);
  assert.doesNotMatch(html, new RegExp(DIGEST));
  assert.doesNotMatch(html, new RegExp(TOKEN));
});

test("the dialog shows why a person is asked: the policy verdict and the evidence gate, as RECAP's text", () => {
  const text = textOf(signedIn(() => modal()));
  assert.match(text, /Why you are asked RECAP's own checks, from trusted configuration: not the model's words\./);
  assert.match(text, /Policy: approval required Reversible write: needs a human approval bound to this exact proposal\. Deciding rule R11-reversible-approval ?\./);
  assert.match(text, /Evidence gate passed VP1-evidence-exists · VP2-target-matches-diagnosis · VP3-action-named · VP4-restore-point/);
  const unloaded = textOf(signedIn(() => modal({ policy: null, check: null })));
  assert.match(unloaded, /A reversible write needs a person's approval bound to this exact proposal\./);
  assert.match(unloaded, /Evidence gate Not loaded yet\./);
});

test("the rationale is the recorded model's untrusted text, labelled with its author", () => {
  const html = signedIn(() => modal());
  assert.match(textOf(html), /Written by the recorded model \(qwen3:8b\), replayed: untrusted text, shown as data\./);
  const quote = tags(html, "q").find((q) => q.attrs.class === "untrusted");
  assert.ok(quote);
  assert.match(quote.attrs.title, /written by the recorded model \(qwen3:8b\), replayed/);
  assert.match(html, /<q class="untrusted"[^>]*>The verified root cause is resource_pressure/);
  // Expected change and rollback hint are the proposer's words too.
  assert.equal(tags(html, "q").filter((q) => q.attrs.class === "untrusted").length, 3);
});

test("the cited evidence: a link to it, where it came from, and its tool output as untrusted text", () => {
  const html = signedIn(() => modal());
  // Markup escapes the & of the query string.
  assert.ok(tags(html, "a").some((a) => a.attrs.href === "#/evidence?run=run-26c5fb3c51&amp;ev=ev-26c5fb-r2i02"));
  const text = textOf(html);
  assert.match(text, /Evidence it cites \(1\)/);
  assert.match(text, /mcp:get_k8s_like_events\(limit=5,service=inventory\)/);
  assert.match(text, /recorded by infrastructure investigator · round 2/);
  assert.match(text, /untrusted · tool output · get_k8s_like_events/);
  assert.match(text, /Container inventory exceeded its memory limit \(768Mi\) and was killed/);
  assert.match(text, /Runbook sections cited: runbook-memory-pressure#remediation-1 shop-architecture#services-1/);
  const missing = textOf(signedIn(() => modal({ evidence: { registry: [] } })));
  assert.match(missing, /ev-26c5fb-r2i02 Not in this run's evidence registry/);
  assert.match(textOf(signedIn(() => modal({ evidence: { registry: null } }))), /Loading the run's evidence…/);
  assert.match(
    textOf(signedIn(() => modal({ evidence: { registry: null, note: "Could not load the run's evidence: Internal Server Error" } }))),
    /Could not load the run's evidence: Internal Server Error/,
  );
});

test("signed in, Approve and Reject are explicit and enabled; Decide later keeps the request pending", () => {
  const html = signedIn(() => modal());
  assert.equal(buttonNamed(html, /^Approve this exact action$/)?.disabled, false);
  assert.equal(buttonNamed(html, /^Reject$/)?.disabled, false);
  assert.equal(buttonNamed(html, /^Decide later$/)?.disabled, false);
  assert.equal(buttonNamed(html, /Test binding: send a tampered digest \(expect 409\)/)?.disabled, false);
  assert.doesNotMatch(textOf(html), /Sign in to decide/);
});

test("signed out, the decision buttons are disabled and the dialog says how to sign in", () => {
  const html = modal();
  assert.equal(buttonNamed(html, /^Approve this exact action$/)?.disabled, true);
  assert.equal(buttonNamed(html, /^Reject$/)?.disabled, true);
  assert.equal(buttonNamed(html, /Test binding/)?.disabled, true);
  assert.equal(buttonNamed(html, /^Decide later$/)?.disabled, false);
  assert.match(textOf(html), /Sign in to decide\. Choose Decide later, sign in \(top right\)/);
});

test("an expired request can no longer be decided: it says so and offers only Close", () => {
  const html = signedIn(() => modal({ request: request({}, -1) }));
  const text = textOf(html);
  assert.match(text, /This request expired at/);
  assert.match(text, /Expired/);
  assert.equal(buttonNamed(html, /^Approve this exact action$/), null);
  assert.equal(buttonNamed(html, /^Close$/)?.disabled, false);
});

test("a request the run closed elsewhere shows why, with Close", () => {
  const html = signedIn(() => modal({ closedReason: "The run ended (Failed) before a decision was made here." }));
  assert.match(textOf(html), /The run ended \(Failed\) before a decision was made here\./);
  assert.equal(buttonNamed(html, /^Reject$/), null);
  assert.ok(buttonNamed(html, /^Close$/));
});

test("a closed dialog renders nothing; hostile proposal text is escaped everywhere", () => {
  assert.equal(modal({ open: false }), "");
  const hostile = request({
    proposal: { ...request().proposal, rationale: HOSTILE, rollback_hint: HOSTILE, expected_health_change: HOSTILE, target: "inventory<script>", citations: [HOSTILE] },
  });
  const html = signedIn(() => modal({ request: hostile, evidence: { registry: [{ ...EVIDENCE, content: HOSTILE, source_ref: HOSTILE }] } }));
  assert.doesNotMatch(html, /<script|<img|href="javascript/);
  assert.match(html, /inventory&lt;script&gt;/);
});

test("the inline approval card (Safety & Approvals) on the simulated shop: one click, as before", () => {
  const html = signedIn(() => render(<ApprovalCard request={request()} environment={ENVIRONMENT} />));
  const text = textOf(html);
  assert.match(text, /Human approval required/);
  assert.match(text, /restart_service on inventory/);
  assert.match(text, /trusted risk: see the policy decision/);
  assert.ok(tags(html, "q").some((q) => q.attrs.class === "untrusted"));
  assert.equal(buttonNamed(html, /^Approve this exact action$/)?.disabled, false);
  assert.ok(buttonNamed(html, /Test binding/));
  // No live block and no name to type on the simulated shop.
  assert.doesNotMatch(text, /LIVE TARGET|to approve/);
  assert.equal(tags(html, "input").length, 0);
  assert.doesNotMatch(html, new RegExp(TOKEN));
  assert.match(textOf(render(<ApprovalCard request={request()} environment={ENVIRONMENT} />)), /Sign in \(top right\)/);
});

test("the inline card asks GET /system for the target when the page names none, and waits for it", () => {
  // Safety & Approvals passes no environment: until GET /system answers, Approve waits; Reject never does.
  const html = signedIn(() => render(<ApprovalCard request={request()} />));
  const approve = tags(html, "button").find((b) => b.attrs.class === "btn btn-approve");
  assert.ok(approve && "disabled" in approve.attrs);
  assert.equal(approve.attrs.title, "Checking which environment this approval acts on (GET /system)…");
  assert.equal(buttonNamed(html, /^Reject$/)?.disabled, false);
  assert.equal(buttonNamed(html, /Test binding/), null, "the binding test is offered on the simulated shop only");
  assert.doesNotMatch(textOf(html), /LIVE TARGET/);
});

// ===============================================================================================
// A live target (Kind, SREGym): the LIVE block and the target service's name, typed
// ===============================================================================================

test("on a live target the inline card shows which real cluster changes and asks for the service's name", () => {
  const html = signedIn(() => render(<ApprovalCard request={liveRequest()} environment={KIND} />));
  const text = textOf(html);
  const block = withAttr(html, "aria-label", "Live target");
  assert.equal(block.length, 1);
  assert.equal(block[0].attrs.role, "note");
  assert.match(text, /LIVE TARGET · kind Kind cluster recap-m5 namespace recap-demo Not a simulation: an approved action changes this real cluster\./);
  assert.match(text, /Approving sends execute_rollback on backend to it once, through the safety gateway\./);
  assert.match(text, /Local Kind cluster recap-m5 \(context kind-recap-m5\), not production/);
  // The typed confirmation: a labelled input, empty, with its status.
  const input = tags(html, "input");
  assert.equal(input.length, 1);
  assert.equal(input[0].attrs.type, "text");
  assert.equal(input[0].attrs.value, "");
  assert.equal(input[0].attrs.autoComplete, "off"); // React writes it in camel case; HTML ignores the case
  assert.equal(input[0].attrs.spellcheck, "false");
  assert.equal(tags(html, "label").filter((l) => l.attrs.for === input[0].attrs.id).length, 1);
  assert.equal(withAttr(html, "id", input[0].attrs["aria-describedby"]).length, 1);
  assert.match(text, /Type backend to approve/);
  assert.match(text, /Approving needs the target service's exact name, typed, as at the console\. Reject needs no name\./);
  // Approve waits for the name; Reject needs none; no binding test on a real cluster.
  assert.equal(buttonNamed(html, /^Approve this exact action$/)?.disabled, true);
  const approve = tags(html, "button").find((b) => b.attrs.class === "btn btn-approve");
  assert.equal(approve?.attrs.title, "Type backend above to enable Approve");
  assert.equal(buttonNamed(html, /^Reject$/)?.disabled, false);
  assert.equal(buttonNamed(html, /Test binding/), null);
  assert.doesNotMatch(text, /simulated shop/);
});

test("the live approval dialog: its title, description and LIVE block name the real cluster; Approve waits for the name", () => {
  const html = signedIn(() => modal({ request: liveRequest(), environment: KIND }));
  const text = textOf(html);
  assert.match(text, /^Approve this exact action on the live target\?/);
  assert.match(text, /This is a live target: nothing changes in Kind cluster recap-m5, namespace recap-demo until you decide, and approving changes a real cluster\./);
  assert.doesNotMatch(text, /simulated shop/);
  assert.equal(withAttr(html, "aria-label", "Live target").length, 1);
  assert.match(text, /LIVE TARGET · kind/);
  assert.match(text, /execute_rollback on backend \(to_version=v1\.0\.0\)/);
  assert.match(text, /Observations from the run's read-only tool calls on Kind cluster recap-m5, namespace recap-demo, as recorded\./);
  // The name sits in the decision bar, right above the buttons.
  assert.match(text, /Type backend to approve .* Decide later Reject Approve this exact action$/);
  assert.equal(buttonNamed(html, /^Approve this exact action$/)?.disabled, true);
  assert.equal(buttonNamed(html, /^Reject$/)?.disabled, false);
  assert.equal(buttonNamed(html, /^Decide later$/)?.disabled, false);
  assert.equal(buttonNamed(html, /Test binding/), null);
  // The dialog still cannot be dismissed, and a screen reader hears the live description.
  const dialog = withAttr(html, "role", "dialog")[0];
  assert.equal(withAttr(html, "aria-label", "Close dialog").length, 0);
  const description = tags(html, "p").find((p) => p.attrs.class === "modal-description");
  assert.ok(description);
  assert.equal(dialog.attrs["aria-describedby"], description.attrs.id);
});

test("SREGym's cluster is named the same way; a signed-out operator can neither type nor approve", () => {
  const sregym = liveRequest({ proposal: { ...liveRequest().proposal, target: "geo", params: { to_version: "sha-d2c036bc3d1138f5a0bdaffd3d87f37522c3461a" } } });
  const html = modal({ request: sregym, environment: SREGYM });
  const text = textOf(html);
  assert.match(text, /LIVE TARGET · sregym SREGym on Kind cluster kind namespace hotel-reservation/);
  assert.match(text, /Type geo to approve/);
  assert.ok("disabled" in tags(html, "input")[0].attrs);
  assert.equal(buttonNamed(html, /^Approve this exact action$/)?.disabled, true);
  assert.equal(buttonNamed(html, /^Reject$/)?.disabled, true);
  assert.match(text, /Sign in to decide/);
});

test("a target nobody reported is treated as live: the name is asked, and the dialog says why", () => {
  const html = signedIn(() => modal({ request: liveRequest(), environment: null }));
  const text = textOf(html);
  assert.match(text, /Target not reported\. GET \/system has not said whether this approval acts on a simulated or a live target, so approving takes the target service's name, as a live target requires\. Reject needs no name\./);
  assert.match(text, /Nothing changes until you decide/);
  assert.doesNotMatch(text, /simulated shop|LIVE TARGET/);
  assert.match(text, /Type backend to approve/);
  assert.equal(buttonNamed(html, /^Approve this exact action$/)?.disabled, true);
  assert.equal(buttonNamed(html, /^Reject$/)?.disabled, false);
  assert.equal(buttonNamed(html, /Test binding/), null);
});

test("the typed name: it matches only the exact target (spaces around it aside), and says so", () => {
  const typed = (value: string) => textOf(render(<ConfirmTargetField service="backend" typed={value} onType={noop} />));
  assert.match(typed("backend"), /Matches the proposal's target: Approve is enabled\./);
  assert.match(typed("  backend "), /Matches the proposal's target/);
  for (const wrong of ["Backend", "backend2", "back end", "frontend", "y"]) {
    assert.match(typed(wrong), /Does not match yet: type the exact name, case included\./, wrong);
  }
  const matched = render(<ConfirmTargetField service="backend" typed="backend" onType={noop} />);
  assert.ok(tags(matched, "div").some((d) => d.attrs.class === "approval-confirm is-named"));
  assert.equal(tags(matched, "input")[0].attrs.value, "backend");
  // A proposal with no target can never be approved here; hostile target text stays text.
  const none = render(<ConfirmTargetField service="" typed="" onType={noop} />);
  assert.match(textOf(none), /The proposal names no target service, so it cannot be approved here\./);
  assert.ok("disabled" in tags(none, "input")[0].attrs);
  const hostile = render(<ConfirmTargetField service={`<img src=x onerror="alert(1)">`} typed="" onType={noop} />);
  assert.equal(tags(hostile, "img").length, 0);
  assert.match(hostile, /&lt;img src=x/);
});

/** Run `body` with fetch answering every request with `status` and `json`; returns the requests seen. */
async function withFetch(status: number, json: unknown, body: () => Promise<void>): Promise<{ url: string; method: string; body: unknown }[]> {
  const seen: { url: string; method: string; body: unknown }[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    seen.push({ url: String(input), method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
    return new Response(JSON.stringify(json), { status, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  try {
    await body();
  } finally {
    globalThis.fetch = original;
  }
  return seen;
}

const LIVE_APPROVAL = {
  approval_id: "apr-84ec57cc5a",
  incident_id: "inc-9d99ba533a",
  proposal_id: "prop-9f461dcb40",
  proposal_digest: "917e98b72d1e4684f6aa77928897a945f90517f991a244aecdb3ae9f9e9c5239",
  scope: "single_action",
  decision: "approved",
  actor: "operator:local-operator",
  decided_at: "2026-11-07T10:00:00Z",
  expires_at: "2026-11-07T10:15:00Z",
  consumed_at: null,
};

test("an approval sends the typed name as confirm_target with the exact digest; a rejection sends no name", async () => {
  const live = liveRequest();
  let outcome: Awaited<ReturnType<typeof sendDecision>> | null = null;
  const sent = await withFetch(200, LIVE_APPROVAL, async () => {
    outcome = await sendDecision(live, { decision: "approved", confirmTarget: " backend " });
  });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].method, "POST");
  assert.equal(sent[0].url, "/approvals/prop-9f461dcb40");
  assert.deepEqual(sent[0].body, { decision: "approved", proposal_digest: live.proposal_digest, confirm_target: "backend" });
  assert.deepEqual(outcome, { ok: true, approval: LIVE_APPROVAL });

  const rejected = await withFetch(200, { ...LIVE_APPROVAL, decision: "rejected" }, async () => {
    await sendDecision(live, { decision: "rejected", confirmTarget: "backend" });
  });
  assert.deepEqual(rejected[0].body, { decision: "rejected", proposal_digest: live.proposal_digest }, "a rejection never sends the name");

  // The simulated shop: no name at all, as before.
  const simulated = await withFetch(200, LIVE_APPROVAL, async () => {
    await sendDecision(request(), { decision: "approved" });
  });
  assert.deepEqual(simulated[0].body, { decision: "approved", proposal_digest: DIGEST });
});

test("the server's refusal (400 without the exact name) is shown as it said it, and the request stays pending", async () => {
  const detail = "on a live target, type the target service's exact name to approve (nothing was decided)";
  let outcome: Awaited<ReturnType<typeof sendDecision>> | null = null;
  const sent = await withFetch(400, { detail }, async () => {
    outcome = await sendDecision(liveRequest(), { decision: "approved", confirmTarget: "Backend" });
  });
  assert.deepEqual((sent[0].body as Record<string, unknown>).confirm_target, "Backend", "the browser never corrects what was typed");
  assert.deepEqual(outcome, { ok: false, status: 400, message: detail, attempt: "approve" });
  const html = render(<DecisionResult outcome={outcome} />);
  assert.equal(withAttr(html, "role", "alert").length, 1);
  assert.equal(
    textOf(html),
    "HTTP 400 for “approve” : on a live target, type the target service's exact name to approve (nothing was decided). The request stays pending.",
  );
  // A hostile message stays text.
  const hostile = render(<DecisionResult outcome={{ ok: false, status: 400, message: HOSTILE, attempt: "approve" }} />);
  assert.doesNotMatch(hostile, /<script|<img/);
});

test("once the server refuses an approval for want of the name, the name is asked, whatever the tab last heard", () => {
  const refused = { ok: false as const, status: 400, message: "on a live target, type the target service's exact name to approve (nothing was decided)", attempt: "approve" };
  const simulated = { target: targetFacts(ENVIRONMENT), checking: false, error: null };
  assert.equal(approveNeedsName(simulated), false);
  assert.equal(approveNeedsName(simulated, refused), true, "a stale 'simulated' never leaves the operator stuck");
  assert.equal(approveNeedsName(simulated, { ...refused, status: 409 }), false, "a digest refusal says nothing about the target");
  assert.equal(approveNeedsName(simulated, { ...refused, attempt: "reject" }), false);
  assert.equal(approveNeedsName({ target: targetFacts(KIND), checking: false, error: null }), true);
  assert.equal(approveNeedsName({ target: targetFacts(null), checking: false, error: "boom" }), true, "not reported: asked, as on a live target");
  assert.equal(approveNeedsName({ target: targetFacts(null), checking: true, error: null }), false, "while GET /system is asked, Approve waits instead");
});

test("a decided live approval shows who the server recorded; nothing is left to type", () => {
  const html = signedIn(() => render(<ApprovalCard request={liveRequest({}, -1)} environment={KIND} />));
  // Expired: the name is no longer asked, and nothing can be decided.
  assert.equal(tags(html, "input").length, 0);
  assert.equal(buttonNamed(html, /^Approve this exact action$/)?.disabled, true);
  assert.equal(buttonNamed(html, /^Reject$/)?.disabled, true);
  const answer = textOf(render(<DecisionResult outcome={{ ok: true, approval: LIVE_APPROVAL as RunRecord["approvals"][number] }} />));
  assert.match(answer, /^Approved by operator:local-operator · the server recorded approval apr-84ec57cc5a/);
});

// ===============================================================================================
// The Lab on a live target: no simulated-shop wording, the cluster named where runs start and end
// ===============================================================================================

const LIVE_SYSTEM = { environment: KIND, environment_kind: "kind", retrieval_mode: "bm25", hybrid_ready: false } as unknown as SystemSummary;
const SIM_SYSTEM = { environment: ENVIRONMENT, environment_kind: "simulated", retrieval_mode: "bm25", hybrid_ready: false } as unknown as SystemSummary;

test("on a live server the Lab says where runs act, before a run starts: header, scenarios, settings and safety", () => {
  const html = signedIn(() => render(<LiveRunPage route={parseHash("#/live")} system={polled(LIVE_SYSTEM)} />));
  const text = textOf(html);
  assert.match(
    text,
    /Start an incident on this server's live target, then watch RECAP investigate, propose a fix, wait for a person's approval and verify the recovery\. The target is Kind cluster recap-m5, namespace recap-demo \(kind\): a real cluster, not a simulation, so an approved fix changes it, and a person approves each fix by typing the target service's name\./,
  );
  assert.doesNotMatch(text, /simulated shop until|acts only on the simulated shop|fault into the simulated shop/);
  // The scenarios are this target's own; where a run acts is shown above them and next to Start run.
  assert.match(text, /This target's scenarios \(GET \/scenarios\), on Kind cluster recap-m5, namespace recap-demo: a real cluster\./);
  assert.match(text, /Runs started here act on LIVE TARGET · kind Kind cluster recap-m5, namespace recap-demo/);
  assert.match(text, /Select a scenario\. on LIVE TARGET · kind Kind cluster recap-m5, namespace recap-demo/);
  // Replays are refused against a live target, and the launcher says why before anyone tries.
  assert.match(text, /No replay against this server's live target This server's target is live \(kind\): RECAP refuses a replay against it before it starts/);
  // Settings: a person always approves (no auto-approver), and there is no simulated latency.
  const approval = withAttr(html, "aria-label", "Approval mode");
  assert.equal(approval.length, 1);
  assert.match(text, /Manual: a person approves here/);
  assert.doesNotMatch(text, /Auto: simulation auto-approver/);
  assert.match(text, /On this live target a person approves the exact action in a dialog, typing the target service's name, before anything changes\. The controller refuses the auto-approver here\./);
  assert.doesNotMatch(text, /Simulated tool latency/);
  assert.equal(withAttr(html, "id", "latency").length, 0);
  // Safety notes: the cluster by name and namespace, and that it is real.
  assert.match(text, /Live target \(kind\)\. Kind cluster recap-m5, namespace recap-demo : a real cluster, not a simulation\. An approved action changes it, and a person approves each one by typing the target service's name\./);
});

test("on the simulated shop the Lab reads as before; before GET /system answers it names no target", () => {
  const html = signedIn(() => render(<LiveRunPage route={parseHash("#/live")} system={polled(SIM_SYSTEM)} />));
  const text = textOf(html);
  assert.match(text, /Replay a recorded real-model incident or start a new one, .* Everything streams from the API and acts only on the simulated shop\./);
  assert.match(text, /Inject a scenario's fault into the simulated shop and let RECAP handle it with the backend you choose \(GET \/scenarios\)/);
  assert.match(text, /Runs started here act on SIMULATED TARGET RECAP simulated shop, namespace shop/);
  assert.match(text, /Auto: simulation auto-approver/);
  assert.match(text, /Simulated tool latency \(ms\)/);
  assert.doesNotMatch(text, /No replay against this server's live target|LIVE TARGET/);

  const waiting = textOf(signedIn(() => render(<LiveRunPage route={parseHash("#/live")} system={polled<SystemSummary>(undefined)} />)));
  assert.match(waiting, /the target shows here once GET \/system reports it\./);
  assert.match(waiting, /Runs started here act on TARGET NOT REPORTED/);
  assert.match(waiting, /Until GET \/system reports the target, a run started here asks a person/);
  assert.doesNotMatch(waiting, /simulated shop\.|LIVE TARGET/);
});

test("the target token: live in red with its place, simulated in amber, unknown said so", () => {
  const live = render(<TargetToken target={targetFacts(KIND)} />);
  assert.match(live, /tone-bad/);
  assert.equal(textOf(live), "LIVE TARGET · kind Kind cluster recap-m5, namespace recap-demo");
  const simulated = render(<TargetToken target={targetFacts(ENVIRONMENT)} size="sm" />);
  assert.match(simulated, /tone-warn/);
  assert.equal(textOf(simulated), "SIMULATED TARGET RECAP simulated shop, namespace shop");
  assert.equal(textOf(render(<TargetToken target={targetFacts(null)} />)), "TARGET NOT REPORTED");
});

test("the safety notes on SREGym's cluster name it, its namespace and its own description", () => {
  const text = textOf(render(<SafetyExplainer environment={SREGYM} />));
  assert.match(text, /Live target \(sregym\)\. SREGym on Kind cluster kind, namespace hotel-reservation : a real cluster, not a simulation\./);
  assert.match(text, /SREGym 1\.1 on its own Kind cluster kind \(context kind-kind\), not production/);
  assert.doesNotMatch(text, /No real system is changed|simulated shop/);
});

test("a live run's banner names the cluster it acts on", () => {
  const text = textOf(render(<RunBannerView banner={runBanner({ origin: "local_llm", kind: "llm", model: "qwen3:8b" }, KIND)} />));
  assert.match(text, /^LIVE LOCAL LLM · LIVE TARGET · kind/);
  assert.match(text, /qwen3:8b, self-hosted \(this machine or the owner's own VM\), makes the decisions now\. It acts on Kind cluster recap-m5, namespace recap-demo: a real cluster, not a simulation, and an approved action changes it\./);
  assert.doesNotMatch(text, /simulated shop/);
});

/** The Kind run's record (run.json): its environment, the owner's approval and the oracle's observations. */
function kindRecord(): RunRecord {
  const health = (service: string, status: string, ready: number, replicas: number, error_rate: number | null) => ({ service, status, ready_replicas: ready, replicas, error_rate });
  return {
    run_id: "run-047aac6eb0",
    scenario_id: "kind-bad-release",
    environment: KIND,
    provider: "ollama-vm:qwen3:8b",
    approvals: [{ ...APPROVED, approval_id: "apr-84ec57cc5a", proposal_id: "prop-9f461dcb40", actor: "console-user" }],
    recovery: {
      healthy_before: false,
      healthy_after: true,
      observed_signals: {
        before: { frontend: health("frontend", "Running", 2, 2, 0.3), backend: health("backend", "Running", 2, 2, 0.3) },
        after: { frontend: health("frontend", "Running", 2, 2, 0.0), backend: health("backend", "Running", 2, 2, 0.0) },
      },
      oracle_name: "kind-health-oracle",
      verified: true,
      checked_at: "2026-11-03T13:13:24.213074Z",
      services_checked: ["frontend", "backend"],
    },
  } as unknown as RunRecord;
}

const KIND_SUMMARY = summary({
  run_id: "run-047aac6eb0",
  scenario_id: "kind-bad-release",
  outcome_note: "oracle verified recovery (kind)",
  provider: "ollama-vm:qwen3:8b",
  provider_class: "local_llm",
  provider_kind: "llm",
  environment_kind: "kind",
  environment_simulated: false,
  wall_seconds: 1303.1,
  replay: null,
  oracle: { oracle: "kind-health-oracle", verified: true, healthy_before: false, healthy_after: true, checked_at: "2026-11-03T13:13:24.213074Z", services_checked: ["frontend", "backend"] },
  labels: ["LIVE ENVIRONMENT (kind)"],
});

const KIND_RECOVERY = { oracle: "kind-health-oracle", healthy_before: false, healthy_after: true, verified: true, unhealthy_after: [], simulated: false, probes: 2, waited_seconds: 10.859 };

test("a resolved live run: the cluster recovered, how long the oracle waited, and what it observed per service", () => {
  const html = render(<OutcomeCard runId="run-047aac6eb0" summary={KIND_SUMMARY} record={kindRecord()} recovery={KIND_RECOVERY} />);
  const text = textOf(html);
  assert.match(text, /Resolved The independent oracle verified that Kind cluster recap-m5, namespace recap-demo recovered: a real cluster, not a simulation\./);
  assert.doesNotMatch(text, /simulated shop/);
  assert.match(text, /Recovery verified unhealthy before → healthy after · kind-health-oracle · checked frontend, backend · live target · 2 probes over 10\.9 s \(settle window 180 s\)/);
  assert.match(text, /approved by console-user used once/);
  assert.match(text, /LOCAL LLM qwen3:8b \/ LIVE TARGET · kind/);
  // The oracle's own observations, before and after the fix, per service.
  assert.match(text, /What the oracle observed .*Service Before the fix After the fix frontend Running · 2\/2 ready · errors 30% Running · 2\/2 ready · errors 0% backend Running · 2\/2 ready · errors 30% Running · 2\/2 ready · errors 0%/);
  const table = tags(html, "table");
  assert.equal(table.length, 1);
  assert.equal(tags(html, "th").filter((th) => th.attrs.scope === "row").length, 2);
  assert.match(html, /<caption class="sr-only">/);
});

test("SREGym's 19 Deployments: the one that changed is shown, the rest fold away; no error rate is invented", () => {
  const services = ["frontend", "search", "geo", "rate", "profile", "recommendation", "reservation", "user", "mongodb-geo", "mongodb-rate", "mongodb-profile", "mongodb-recommendation", "mongodb-reservation", "mongodb-user", "memcached-rate", "memcached-profile", "memcached-reserve", "consul", "jaeger"];
  const side = (geoStatus: string, geoReady: number) =>
    Object.fromEntries(services.map((s) => [s, { service: s, status: s === "geo" ? geoStatus : "Running", ready_replicas: s === "geo" ? geoReady : 1, replicas: 1, error_rate: null }]));
  const observed = { before: side("CrashLoopBackOff", 0), after: side("Running", 1) };
  const rows = recoverySignals(observed);
  assert.equal(rows.length, 19);
  assert.deepEqual(rows[0], {
    service: "geo",
    before: { status: "CrashLoopBackOff", ready: 0, replicas: 1, errorRate: null },
    after: { status: "Running", ready: 1, replicas: 1, errorRate: null },
    changed: true,
  });
  const html = render(<ObservedSignals rows={rows} />);
  const text = textOf(html);
  assert.match(text, /geo CrashLoopBackOff · 0\/1 ready Running · 1\/1 ready/);
  assert.match(text, /18 other services unchanged/);
  assert.doesNotMatch(text, /errors/, "SREGym's app measures no request errors: none is shown");
  assert.equal(tags(html, "details").length, 1);
  // The unready side is marked in words and color, not color alone.
  assert.match(html, /<span class="text-warn">CrashLoopBackOff · 0\/1 ready<\/span>/);
  assert.equal(render(<ObservedSignals rows={[]} />), "");
});

test("an older run without probes still reads its oracle verdict; a simulated run never claims a live target", () => {
  const simulated = textOf(render(<OutcomeCard runId="run-26c5fb3c51" summary={summary()} record={recordWith([APPROVED])} recovery={{ simulated: true, probes: 1, waited_seconds: 0 }} />));
  assert.match(simulated, /simulated-health-oracle · checked cart, checkout, frontend, inventory, payment · simulated target · 1 probe/);
  assert.doesNotMatch(simulated, /live target|LIVE/);
  const older = textOf(render(<OutcomeCard runId="run-047aac6eb0" summary={KIND_SUMMARY} record={kindRecord()} />));
  assert.match(older, /checked frontend, backend Approval /);
  assert.match(older, /What the oracle observed/);
});
