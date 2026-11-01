/**
 * Component tests for the Incident Lab (src/pages/LiveRun.tsx and the components it owns:
 * ApprovalCard / ApprovalModal, EventFeed, StateStepper). Rendered on the server, so they check
 * what each view shows for given data: labels, roles, aria attributes, escaping, and the
 * loading / empty / error states. The data is shaped exactly like the API's JSON (src/dto.ts,
 * src/types.ts): GET /replays and GET /providers from a seeded server, and a paced replay of the
 * golden qwen3:8b recording against the simulated shop, stopped at its approval request.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../src/api";
import { setToken } from "../src/auth";
import { ApprovalCard, ApprovalModal } from "../src/components/ApprovalCard";
import type { CitedEvidence } from "../src/components/ApprovalCard";
import { EventFeed } from "../src/components/EventFeed";
import { StateStepper } from "../src/components/StateStepper";
import type * as dto from "../src/dto";
import {
  BackendChoice,
  InspectLinks,
  OutcomeCard,
  ReplayLauncher,
  RunBannerView,
  SafetyExplainer,
  SignInPrompt,
} from "../src/pages/LiveRun";
import type { ApprovalRequest, EnvironmentInfo, RunRecord, TraceEvent } from "../src/types";
import { runBanner } from "../src/view/lab";
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
      {...over}
    />,
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

test("the inline approval card (Safety & Approvals) still renders from the request alone", () => {
  const html = signedIn(() => render(<ApprovalCard request={request()} />));
  const text = textOf(html);
  assert.match(text, /Human approval required/);
  assert.match(text, /restart_service on inventory/);
  assert.match(text, /trusted risk: see the policy decision/);
  assert.ok(tags(html, "q").some((q) => q.attrs.class === "untrusted"));
  assert.equal(buttonNamed(html, /^Approve this exact action$/)?.disabled, false);
  assert.ok(buttonNamed(html, /Test binding/));
  assert.doesNotMatch(html, new RegExp(TOKEN));
  assert.match(textOf(render(<ApprovalCard request={request()} />)), /Sign in \(top right\)/);
});
