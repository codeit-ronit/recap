/**
 * View helpers for the Incident Lab (pages/LiveRun.tsx): the target a run acts on (simulated or a
 * live cluster), the replay launcher (GET /replays), the backend choice (GET /providers), the
 * run's data-source banner, the stage stepper, the approval state of a run and the body of a
 * decision, its outcome, the oracle's verdict and what it observed, and the bounded live feed.
 * The target helpers are shared with Trace Explorer, Evidence & Decisions and the approval card.
 *
 * Every label is built from what the server reported; a value it did not report reads
 * NOT_RECORDED, never a guess. Text a model or a tool wrote passes through unchanged: the
 * components render it escaped and labelled with its author.
 *
 * Pure functions with type-only imports, so tests/incident-lab.test.ts loads this file directly.
 */

import type { IconName } from "../components/Icon";
import type { SourceFields } from "../components/DataSourceBadge";
import type { FixtureView, OracleVerdict, ProviderView, TextAuthor } from "../dto";
import type { Tone } from "../format";
import type {
  ApprovalDecisionBody,
  EnvironmentInfo,
  ExperienceEpisode,
  ProviderInfo,
  RunConfigInput,
  ScenarioInfo,
  TraceEvent,
} from "../types";

/** What a value the server did not report reads as. */
export const NOT_RECORDED = "Not recorded";

// --- small readers (local copies: this module imports no runtime values) -------------------------

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/** Parse a Python ISO timestamp (microseconds trimmed to milliseconds), NaN when absent. */
export function isoMs(iso: string | null | undefined): number {
  if (!iso) return Number.NaN;
  return Date.parse(iso.replace(/(\.\d{3})\d+/, "$1"));
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

// ==================================================================================================
// The target: where a run's actions land (GET /system `environment`, or the run's own)
// ==================================================================================================

/** The environment fields a target is read from: GET /system `environment`, or a run's own. */
export type TargetFields = Partial<
  Pick<EnvironmentInfo, "is_simulated" | "kind" | "name" | "namespace" | "description" | "stabilization_timeout_seconds">
>;

/** Where actions land, as the server reported it. "unknown": nothing was reported (never guessed). */
export interface TargetFacts {
  state: "simulated" | "live" | "unknown";
  /** The adapter's kind (simulated, kind, sregym...); "" when not reported. */
  kind: string;
  name: string;
  namespace: string;
  /** The adapter's own description, from the target profile (RECAP's configuration). */
  description: string;
  /** How long the oracle lets a live target settle after a fix (seconds), when reported. */
  settleSeconds: number | null;
}

export function targetFacts(environment: TargetFields | null | undefined): TargetFacts {
  let state: TargetFacts["state"] = "unknown";
  if (environment && typeof environment.is_simulated === "boolean") state = environment.is_simulated ? "simulated" : "live";
  const settle = count(environment?.stabilization_timeout_seconds);
  return {
    state,
    kind: text(environment?.kind),
    name: text(environment?.name),
    namespace: text(environment?.namespace),
    description: text(environment?.description),
    settleSeconds: settle ? settle : null,
  };
}

/** "Kind cluster recap-m5, namespace recap-demo": what the server named of the target ("" if nothing). */
export function targetPlace(target: TargetFacts): string {
  return [target.name, target.namespace ? `namespace ${target.namespace}` : ""].filter(Boolean).join(", ");
}

/**
 * A live target in a few words: its name and namespace when reported, else its kind. Both live
 * adapters (Kind, SREGym) are Kubernetes clusters with a namespace, so "real cluster"; a live
 * target that reports no namespace is called a real system.
 */
export function liveTargetWords(target: TargetFacts): { place: string; real: string } {
  return {
    place: targetPlace(target) || `the live ${target.kind || "target"}`,
    real: target.namespace ? "a real cluster" : "a real system",
  };
}

/**
 * The environment an approval acts on: the run's own (its first event or record) and the
 * server's (GET /system, what the server checks a decision against). When either is not
 * simulated, the live one: a typed name is never skipped because one source said "simulated".
 * null when neither is known.
 */
export function approvalTarget(run: TargetFields | null | undefined, server: TargetFields | null | undefined): TargetFields | null {
  const known = [run, server].filter((e): e is TargetFields => !!e && typeof e.is_simulated === "boolean");
  return known.find((e) => e.is_simulated === false) ?? known[0] ?? null;
}

/** A typed run summary's two target fields (GET /runs), as TargetFields. */
export function summaryTarget(
  summary: { environment_kind?: string | null; environment_simulated?: boolean | null } | null | undefined,
): TargetFields | null {
  if (!summary || typeof summary.environment_simulated !== "boolean") return null;
  return { is_simulated: summary.environment_simulated, kind: summary.environment_kind ?? undefined };
}

// ==================================================================================================
// Replays: GET /replays
// ==================================================================================================

/** How fast a replay plays back its recorded answers (RunConfig.replay_pace, 0..1). */
export interface PaceOption {
  key: "watchable" | "instant";
  value: number;
  label: string;
  hint: string;
}

export const REPLAY_PACES: readonly PaceOption[] = [
  {
    key: "watchable",
    value: 0.05,
    label: "Watchable (about half a minute)",
    hint: "Each recorded answer waits 5% of the time the model originally took, so every stage stays on screen long enough to follow. Only the timing changes, never what is replayed.",
  },
  {
    key: "instant",
    value: 0,
    label: "Instant",
    hint: "Recorded answers come back at once, so the run reaches your approval within seconds. Only the timing changes, never what is replayed.",
  },
];

/** The default pace: watchable. */
export const DEFAULT_PACE = 0.05;

/** RunConfig.replay_pace accepts 0 to 1 (recap/core/models.py); anything else becomes the nearest bound. */
export function clampPace(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  return value >= 1 ? 1 : value;
}

/** The body of POST /incidents/run for a replay: what api.startReplay sends, plus the pace. */
export interface ReplayRunBody {
  scenario_id: string;
  config: {
    provider: "replay";
    replay_fixture: string;
    /** A replay started here always asks a person: the dashboard never auto-approves it. */
    approval_mode: "manual";
    replay_pace: number;
  };
}

/**
 * Exactly the fixture's scenario and id, manual approval and the pace. Routing, memory and
 * retrieval are left out on purpose: the server takes them from the recording's manifest entry
 * and refuses a replay that asks anything else.
 */
export function replayRunBody(fixture: Pick<FixtureView, "id" | "scenario">, pace: number): ReplayRunBody {
  return {
    scenario_id: fixture.scenario,
    config: { provider: "replay", replay_fixture: fixture.id, approval_mode: "manual", replay_pace: clampPace(pace) },
  };
}

export interface StatusView {
  label: string;
  tone: Tone;
  icon: IconName;
  detail: string;
}

/** A recording's bytes against the SHA-256 its manifest entry pins (FixtureView.integrity). */
export function integrityView(integrity: string | null | undefined, sha256?: string | null): StatusView {
  const pinned = text(sha256) ? ` (sha256 ${text(sha256)}…)` : "";
  switch (integrity) {
    case "intact":
      return { label: "Intact", tone: "ok", icon: "check", detail: `Its bytes match the SHA-256 the manifest pins${pinned}.` };
    case "altered":
      return {
        label: "Altered",
        tone: "bad",
        icon: "x",
        detail: `Its bytes do not match the SHA-256 the manifest pins${pinned}: the server never replays an altered recording.`,
      };
    case "missing":
      return { label: "Missing", tone: "bad", icon: "x", detail: "The recording file is not in this checkout." };
    default:
      return { label: NOT_RECORDED, tone: "neutral", icon: "minus", detail: "The server did not report this recording's integrity." };
  }
}

/** Whether the server lets this recording be replayed now, and its reasons when not. */
export function launchView(fixture: Pick<FixtureView, "launchable" | "not_launchable_because">): { launchable: boolean; reasons: string[] } {
  if (fixture.launchable === true) return { launchable: true, reasons: [] };
  const reasons = list(fixture.not_launchable_because).map(text).filter(Boolean);
  return { launchable: false, reasons: reasons.length ? reasons : ["The server did not say why."] };
}

/**
 * The data source a replay of this recording has: a replay (no model is called) of answers that
 * the named provider's model gave. Read from the fixture's own fields, never from its title.
 */
export function fixtureSource(fixture: Pick<FixtureView, "model" | "provider" | "recording_id">): SourceFields {
  const model = text(fixture.model) || null;
  return {
    origin: "replay",
    kind: "replay",
    model,
    replay_of: {
      original_provider: text(fixture.provider) || null,
      model,
      recording_id: text(fixture.recording_id) || null,
    },
  };
}

/** "parallel routing · memory on · bm25 search", from the recording's manifest entry. */
export function fixtureRouting(fixture: Pick<FixtureView, "strategy" | "use_memory" | "retrieval">): string {
  const parts = [
    text(fixture.strategy) ? `${text(fixture.strategy)} routing` : null,
    typeof fixture.use_memory === "boolean" ? `memory ${fixture.use_memory ? "on" : "off"}` : null,
    text(fixture.retrieval) ? `${text(fixture.retrieval)} search` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : NOT_RECORDED;
}

/** "225,578 bytes" (or NOT_RECORDED). */
export function fmtBytes(bytes: unknown): string {
  return typeof bytes === "number" && Number.isFinite(bytes) && bytes >= 0 ? `${bytes.toLocaleString("en-US")} bytes` : NOT_RECORDED;
}

// ==================================================================================================
// Backends: GET /providers
// ==================================================================================================

export interface ProviderChoices {
  /** Selectable for a run started from the scenario form (the server's `selectable`), default first. */
  choosable: ProviderView[];
  /** The replay provider: it needs a recording, so it is launched from the replay panel. */
  replay: ProviderView | null;
  /** Not selectable for one run, each with the server's own reason (`selection_rule`). */
  unavailable: { provider: ProviderView; reason: string }[];
  /** The server's default provider, if it reported one. */
  defaultName: string | null;
}

export function providerChoices(providers: readonly ProviderView[]): ProviderChoices {
  const all = list(providers) as ProviderView[];
  const defaultName = all.find((p) => p.default === true)?.name ?? null;
  const choosable: ProviderView[] = [];
  const unavailable: { provider: ProviderView; reason: string }[] = [];
  let replay: ProviderView | null = null;
  for (const provider of all) {
    if (provider.kind === "replay" && provider.selectable === true) {
      replay = provider;
    } else if (provider.selectable === true) {
      choosable.push(provider);
    } else {
      unavailable.push({ provider, reason: text(provider.selection_rule) || "The server did not say why." });
    }
  }
  choosable.sort((a, b) => Number(b.default === true) - Number(a.default === true));
  return { choosable, replay, unavailable, defaultName };
}

/** The backend preselected in the form: the server's default when it can be chosen, else the first. */
export function initialProvider(choices: ProviderChoices): string | null {
  if (choices.defaultName && choices.choosable.some((p) => p.name === choices.defaultName)) return choices.defaultName;
  return choices.choosable[0]?.name ?? null;
}

/** A provider's readiness as GET /providers reports it (ready, needs_setup, stopped, disabled, unavailable). */
export function readinessView(status: string | null | undefined, detail?: string | null): StatusView {
  const why = text(detail);
  switch (status) {
    case "ready":
      return { label: "Ready", tone: "ok", icon: "check", detail: why };
    case "needs_setup":
      return { label: "Needs setup", tone: "warn", icon: "alert", detail: why };
    case "stopped":
      return { label: "Stopped", tone: "bad", icon: "stop", detail: why };
    case "disabled":
      return { label: "Disabled", tone: "neutral", icon: "minus", detail: why };
    case "unavailable":
      return { label: "Unavailable", tone: "bad", icon: "x", detail: why };
    default:
      return { label: text(status) ? text(status).replace(/_/g, " ") : NOT_RECORDED, tone: "neutral", icon: "info", detail: why };
  }
}

/**
 * The RunConfig a scenario run sends. `provider` is sent only when the operator chose a backend
 * other than the server's default; left out, the server's default applies (as before M4).
 *
 * Against a target that is not simulated (or not reported yet) a person always approves: the
 * controller refuses the auto-approver there, so "auto" is never sent. Nor is a simulated tool
 * latency: a real cluster has its own (the Kubernetes adapters accept the knob and ignore it).
 */
export function runConfigBody(
  config: RunConfigInput,
  chosen: string | null,
  defaultName: string | null,
  target?: TargetFacts | null,
): RunConfigInput {
  const body: RunConfigInput = { ...config };
  delete body.provider;
  delete body.replay_fixture;
  if (chosen && chosen !== defaultName) body.provider = chosen;
  if (target && target.state !== "simulated") {
    body.approval_mode = "manual";
    body.tool_latency_ms = null;
  }
  return body;
}

// ==================================================================================================
// Scenarios: GET /scenarios
// ==================================================================================================

const PARTITION_ORDER: Readonly<Record<string, number>> = { demo: 0, train: 1, test: 2 };

/** Demo scenarios first, then training and held-out ones, each group in the server's order. */
export function sortScenarios(scenarios: readonly ScenarioInfo[]): ScenarioInfo[] {
  return [...scenarios].sort((a, b) => (PARTITION_ORDER[a.partition] ?? 9) - (PARTITION_ORDER[b.partition] ?? 9));
}

/** A scenario may pin some run settings (e.g. a strategy); pre-fill the form with them. */
export function withOverrides(config: RunConfigInput, scenario: Pick<ScenarioInfo, "run_overrides">): RunConfigInput {
  const o = record(scenario.run_overrides);
  const next = { ...config };
  if (o.strategy === "adaptive" || o.strategy === "single" || o.strategy === "parallel") next.strategy = o.strategy;
  if (typeof o.use_rag === "boolean") next.use_rag = o.use_rag;
  if (typeof o.use_memory === "boolean") next.use_memory = o.use_memory;
  if (o.approval_mode === "manual" || o.approval_mode === "auto") next.approval_mode = o.approval_mode;
  if (typeof o.tool_latency_ms === "number") next.tool_latency_ms = o.tool_latency_ms;
  return next;
}

// --- the lab's words for the server's target (GET /system) -----------------------------------------

/** The Incident Lab's description, for the target this server's runs act on. */
export function labIntro(environment: TargetFields | null | undefined): string {
  const watch = "then watch RECAP investigate, propose a fix, wait for a person's approval and verify the recovery.";
  const target = targetFacts(environment);
  if (target.state === "simulated") {
    return `Replay a recorded real-model incident or start a new one, ${watch} Everything streams from the API and acts only on the simulated shop.`;
  }
  if (target.state === "live") {
    const { place, real } = liveTargetWords(target);
    return `Start an incident on this server's live target, ${watch} The target is ${place} (${target.kind || "kind not reported"}): ${real}, not a simulation, so an approved fix changes it, and a person approves each fix by typing the target service's name. Everything streams from the API.`;
  }
  return `Replay a recorded real-model incident or start a new one, ${watch} Everything streams from the API; the target shows here once GET /system reports it.`;
}

/** The scenario card's subtitle: the scenarios are the configured target's own (GET /scenarios). */
export function scenarioIntro(environment: TargetFields | null | undefined): string {
  const target = targetFacts(environment);
  if (target.state === "simulated") {
    return "Inject a scenario's fault into the simulated shop and let RECAP handle it with the backend you choose (GET /scenarios)";
  }
  if (target.state === "live") {
    const { place, real } = liveTargetWords(target);
    return `This target's scenarios (GET /scenarios), on ${place}: ${real}. Each says how its fault arises; RECAP handles it with the backend you choose`;
  }
  return "The scenarios of the server's target (GET /scenarios), handled by RECAP with the backend you choose";
}

/**
 * Why no recording can be replayed here, when the target is not simulated: the controller
 * refuses a replay against a live target before it starts (a recorded model's fix must never
 * reach anything real). null on the simulated shop or before GET /system answers.
 */
export function replayBlockedNote(environment: TargetFields | null | undefined): string | null {
  const target = targetFacts(environment);
  if (target.state !== "live") return null;
  return `This server's target is live (${target.kind || "kind not reported"}): RECAP refuses a replay against it before it starts, because a recorded model's fix must never reach a real system. Replays run on a server whose target is the simulated shop.`;
}

/** Demo prerequisites (e.g. memory-caution needs a verified bad-deployment episode in memory). */
export function missingPrerequisites(
  scenario: Pick<ScenarioInfo, "requires_memory_of">,
  episodes: readonly Pick<ExperienceEpisode, "scenario_id" | "outcome" | "verified">[] | undefined,
): string[] {
  if (!episodes) return [];
  return list(scenario.requires_memory_of)
    .map(text)
    .filter((needed) => needed && !episodes.some((ep) => ep.scenario_id === needed && ep.outcome === "success" && ep.verified));
}

// ==================================================================================================
// The run: data-source banner, stages, approval, outcome
// ==================================================================================================

export interface RunBanner {
  /** e.g. "RECORDED REAL-LLM RUN · SIMULATED TARGET" */
  label: string;
  decisions: string;
  target: string;
  tone: Tone;
  icon: IconName;
  /** One plain sentence for a newcomer: where the decisions come from and what they act on. */
  explanation: string;
}

/**
 * The run's prominent data-source label, from the provider and environment the run itself
 * recorded (its first trace event, or its record). Nothing is inferred from a name.
 */
export function runBanner(
  provider: Partial<Pick<ProviderInfo, "origin" | "kind" | "model" | "replay_of">> | null | undefined,
  environment: TargetFields | null | undefined,
): RunBanner {
  const origin = provider?.origin ?? (provider?.kind === "scripted" || provider?.kind === "replay" ? provider.kind : undefined);
  const of = record(provider?.replay_of);
  const model = text(of.model) || text(provider?.model);
  let decisions: string;
  let tone: Tone;
  let icon: IconName;
  let why: string;
  switch (origin) {
    case "replay": {
      decisions = "RECORDED REAL-LLM RUN";
      tone = "warn";
      icon = "rewind";
      const by = [model, text(of.original_provider) ? `via ${text(of.original_provider)}` : ""].filter(Boolean).join(" ");
      why = `The answers ${by || "a real model"} gave in an earlier run are replayed offline: no model is called now.`;
      break;
    }
    case "scripted":
      decisions = "SCRIPTED BASELINE (NOT AN LLM)";
      tone = "neutral";
      icon = "code";
      why = "Fixed playbooks written for the simulated incidents make the decisions: no model is called.";
      break;
    case "local_llm":
      decisions = "LIVE LOCAL LLM";
      tone = "accent";
      icon = "cpu";
      why = `${model || "A model"}, self-hosted (this machine or the owner's own VM), makes the decisions now.`;
      break;
    case "free_hosted_llm":
      decisions = "LIVE FREE HOSTED LLM";
      tone = "info";
      icon = "cloud";
      why = `${model || "A hosted model"} on a free plan makes the decisions now.`;
      break;
    case "paid_llm":
      decisions = "PAID LLM";
      tone = "bad";
      icon = "alert";
      why = "A billed API makes the decisions (this project keeps it disabled).";
      break;
    default:
      decisions = "SOURCE NOT RECORDED";
      tone = "neutral";
      icon = "minus";
      why = "The run did not record where its decisions came from.";
  }
  let target: string;
  let where: string;
  const facts = targetFacts(environment);
  if (facts.state === "unknown") {
    target = "TARGET NOT RECORDED";
    where = "The run did not record which environment it acts on.";
  } else if (facts.state === "simulated") {
    target = "SIMULATED TARGET";
    where = "Every action changes only RECAP's simulated shop.";
  } else {
    const live = liveTargetWords(facts);
    target = `LIVE TARGET · ${facts.kind || "kind not reported"}`;
    where = `It acts on ${live.place}: ${live.real}, not a simulation, and an approved action changes it.`;
    tone = "bad";
    icon = "alert";
  }
  return { label: `${decisions} · ${target}`, decisions, target, tone, icon, explanation: `${why} ${where}` };
}

/** What each lifecycle stage does, in one plain sentence (RECAP's own description), for any target. */
export const STAGE_HELP: Readonly<Record<string, string>> = {
  CREATED: "The alert became an incident with its own budget.",
  RETRIEVING: "RECAP searches the runbooks and its memory of past incidents for context.",
  INVESTIGATING: "Agents call read-only tools on the target and propose diagnoses; the verifier checks each one against the evidence.",
  PROPOSING: "A fix is drafted from the verified diagnosis and checked by the evidence gate.",
  AWAITING_APPROVAL: "The safety policy needs a person to approve this exact action before anything changes.",
  MITIGATING: "The approved action goes through the safety gateway to the target, once.",
  VERIFYING: "An independent oracle checks whether the target recovered.",
  RESOLVED: "The independent oracle verified the recovery.",
  FAILED: "The run stopped without a verified recovery.",
  BLOCKED: "The fix did not run: the policy or the evidence gate refused it, a person rejected it, or the approval expired.",
  BUDGET_EXHAUSTED: "A run limit was reached, so the run stopped safely.",
  QUOTA_EXHAUSTED: "A free model's quota ran out: the run was deferred and nothing was decided.",
};

/**
 * What a stage does on the run's own target (its first event or record): the simulated shop, a
 * live cluster by name, or (target not recorded) the target-neutral sentence of STAGE_HELP.
 */
export function stageHelp(state: string | null | undefined, environment: TargetFields | null | undefined): string {
  const base = STAGE_HELP[state ?? ""] ?? "";
  const target = targetFacts(environment);
  if (target.state === "simulated") {
    const simulated: Record<string, string> = {
      INVESTIGATING: "Agents call read-only tools on the simulated shop and propose diagnoses; the verifier checks each one against the evidence.",
      MITIGATING: "The approved action goes through the safety gateway to the simulated shop, once.",
      VERIFYING: "An independent oracle checks whether the shop recovered.",
    };
    return simulated[state ?? ""] ?? base;
  }
  if (target.state === "live") {
    const { place, real } = liveTargetWords(target);
    const settle = target.settleSeconds !== null ? ` (up to ${target.settleSeconds} s)` : "";
    const live: Record<string, string> = {
      INVESTIGATING: `Agents call read-only tools on ${place}, ${real}, and propose diagnoses; the verifier checks each one against the evidence.`,
      AWAITING_APPROVAL: `The safety policy needs a person to approve this exact action before anything changes. On this live target, approving takes the target service's name, typed.`,
      MITIGATING: `The approved action goes through the safety gateway to ${place}, once: this changes ${real}.`,
      VERIFYING: `An independent oracle probes ${place} until its services are healthy or the target's settle window${settle} ends.`,
    };
    return live[state ?? ""] ?? base;
  }
  return base;
}

/** The states a run entered, in order (state.transition events). */
export function statesOf(events: readonly TraceEvent[]): string[] {
  const states: string[] = [];
  for (const event of events) {
    if (event.category !== "state.transition") continue;
    const to = text(record(event.payload).to);
    if (to) states.push(to);
  }
  return states;
}

/** When the run first entered each state, in ms after its first event. */
export function stageTimes(events: readonly TraceEvent[]): Record<string, number> {
  const times: Record<string, number> = {};
  if (!events.length) return times;
  const t0 = isoMs(events[0].ts);
  for (const event of events) {
    if (event.category !== "state.transition") continue;
    const to = text(record(event.payload).to);
    const at = isoMs(event.ts) - t0;
    if (to && !(to in times) && Number.isFinite(at)) times[to] = Math.max(0, at);
  }
  return times;
}

/** "+12.3 s" after the run's first event. */
export function fmtOffset(ms: number | null | undefined): string {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return "";
  if (ms < 10_000) return `+${(ms / 1000).toFixed(1)} s`;
  if (ms < 60_000) return `+${Math.round(ms / 1000)} s`;
  return `+${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
}

/** Where a run stands with its approval, from its trace. */
export type ApprovalPhase =
  | { kind: "none" }
  /** A request is open. `auto`: the simulation auto-approver decides, not a person. */
  | { kind: "waiting"; auto: boolean; proposalId: string | null; expiresAt: string | null; channel: string }
  /**
   * The last request was decided: approved, rejected, or expired. `note`: RECAP's record of how
   * the answer came (e.g. "dashboard or API; typed the target service's name; answered 41.2 s
   * after the request"), when the decision log kept one.
   */
  | {
      kind: "decided";
      auto: boolean;
      decision: string;
      actor: string | null;
      approvalId: string | null;
      proposalId: string | null;
      note: string | null;
    };

export function approvalPhase(events: readonly TraceEvent[]): ApprovalPhase {
  let requested: TraceEvent | null = null;
  let decided: TraceEvent | null = null;
  for (const event of events) {
    if (event.category === "approval.requested") requested = event;
    else if (event.category === "approval.decided") decided = event;
  }
  if (!requested) return { kind: "none" };
  const asked = record(requested.payload);
  const channel = text(asked.channel);
  const auto = /auto/i.test(channel);
  if (decided && decided.seq > requested.seq) {
    const said = record(decided.payload);
    return {
      kind: "decided",
      auto,
      decision: text(said.decision) || NOT_RECORDED,
      actor: text(said.actor) || null,
      approvalId: text(said.approval_id) || null,
      proposalId: text(said.proposal_id) || text(asked.proposal_id) || null,
      note: text(said.note) || null,
    };
  }
  return { kind: "waiting", auto, proposalId: text(asked.proposal_id) || null, expiresAt: text(asked.expires_at) || null, channel };
}

// --- deciding: the body of POST /approvals/{proposal_id} ---------------------------------------------

/**
 * Whether the typed text names the proposal's target exactly, as the server compares it
 * (apps/api/main.py: surrounding spaces dropped, then equal, case included) and as the console
 * does on a live target. An empty target is never named.
 */
export function namesTarget(typed: string | null | undefined, target: string | null | undefined): boolean {
  return typeof target === "string" && target !== "" && typeof typed === "string" && typed.trim() === target;
}

/** Flip the last hex digit: a digest for a *different* proposal than the one shown. */
export function tamper(digest: string): string {
  if (!digest) return "0";
  return digest.slice(0, -1) + (digest.endsWith("0") ? "1" : "0");
}

/** One decision a person sends from this tab. */
export interface DecisionChoice {
  decision: "approved" | "rejected";
  /** The binding test: send a digest for a different proposal (the server must refuse it, 409). */
  tampered?: boolean;
  /** On a live target: the target service's name, as the person typed it. */
  confirmTarget?: string | null;
}

/**
 * The body of POST /approvals/{proposal_id}: the decision and the digest of the exact proposal
 * shown. An approval carries the name the person typed (`confirm_target`), which a target that is
 * not simulated requires; a rejection never needs it and never sends it. Who decided comes from
 * the operator token, never from the body.
 */
export function decisionBody(digest: string, choice: DecisionChoice): ApprovalDecisionBody {
  const body: ApprovalDecisionBody = { decision: choice.decision, proposal_digest: choice.tampered ? tamper(digest) : digest };
  const typed = typeof choice.confirmTarget === "string" ? choice.confirmTarget.trim() : "";
  if (choice.decision === "approved" && typed) body.confirm_target = typed;
  return body;
}

/** The attempt in words, for the server's answer ("HTTP 400 for “approve”: ..."). */
export function attemptLabel(choice: DecisionChoice): string {
  if (choice.tampered) return "approve with a tampered digest";
  return choice.decision === "approved" ? "approve" : "reject";
}

export interface ApproveGate {
  /** The Approve button is disabled. */
  blocked: boolean;
  /** Why, when it waits for the name or for GET /system (shown on the button). */
  why: string | null;
}

/**
 * Whether Approve can be pressed. `canDecide`: signed in, nothing being sent, the request still
 * open. `checking`: GET /system has not said yet whether the target is simulated. `needsName`:
 * the target is live (or not reported), so the person must type the proposal's target service,
 * exactly (namesTarget). Reject is never gated by the name.
 */
export function approveGate(opts: { canDecide: boolean; checking: boolean; needsName: boolean; typed: string; service: string }): ApproveGate {
  if (!opts.canDecide) return { blocked: true, why: null };
  if (opts.checking) return { blocked: true, why: "Checking which environment this approval acts on (GET /system)…" };
  if (opts.needsName && !namesTarget(opts.typed, opts.service)) {
    return { blocked: true, why: `Type ${opts.service || "the target service's name"} above to enable Approve` };
  }
  return { blocked: false, why: null };
}

/** The trusted risk tier the registry gave a proposal (its mitigation.proposed event), if traced. */
export function trustedRiskOf(events: readonly TraceEvent[], proposalId: string | null | undefined): string | null {
  if (!proposalId) return null;
  for (let i = events.length - 1; i >= 0; i -= 1) {
    if (events[i].category !== "mitigation.proposed") continue;
    const payload = record(events[i].payload);
    if (text(record(payload.proposal).proposal_id) === proposalId) return text(payload.trusted_risk) || null;
  }
  return null;
}

/**
 * Who wrote a proposal's rationale, in words: the server's TextAuthor for it (GET /runs/{id}/evidence
 * proposals[].author) when known, else the run's provider.
 */
export function rationaleAuthor(
  author: TextAuthor | string | null | undefined,
  provider?: Partial<Pick<ProviderInfo, "origin" | "kind" | "model" | "replay_of">> | null,
): { who: string; trusted: boolean } {
  const model = text(record(provider?.replay_of).model) || text(provider?.model);
  const named = model ? ` (${model})` : "";
  switch (author) {
    case "model":
      return { who: `the model${named}`, trusted: false };
    case "replayed_model":
      return { who: `the recorded model${named}, replayed`, trusted: false };
    case "scripted_policy":
      return { who: "the scripted policy", trusted: false };
    case "recap":
      return { who: "RECAP", trusted: true };
    default: {
      const origin = provider?.origin ?? provider?.kind;
      if (origin === "replay") return { who: `the recorded model${named}, replayed`, trusted: false };
      if (origin === "scripted") return { who: "the scripted policy", trusted: false };
      if (origin === "local_llm" || origin === "free_hosted_llm" || origin === "paid_llm" || origin === "llm") {
        return { who: `the model${named}`, trusted: false };
      }
      return { who: "the proposer", trusted: false };
    }
  }
}

export interface OutcomeView {
  label: string;
  tone: Tone;
  icon: IconName;
  sentence: string;
}

/** The resolved sentence for the run's target: the simulated shop, a live cluster by name, or neither named. */
function recoveredSentence(environment: TargetFields | null | undefined): string {
  const target = targetFacts(environment);
  if (target.state === "simulated") return "The independent oracle verified that the simulated shop recovered.";
  if (target.state === "live") {
    const { place, real } = liveTargetWords(target);
    return `The independent oracle verified that ${place} recovered: ${real}, not a simulation.`;
  }
  return "The independent oracle verified the recovery.";
}

/**
 * How a run ended (GET /runs/{id} summary.outcome, apps/api/dto.py RunOutcome), in plain words.
 * `environment`: the run's own target, which the resolved sentence names.
 */
export function outcomeView(outcome: string | null | undefined, environment?: TargetFields | null): OutcomeView {
  switch (outcome) {
    case "resolved":
      return { label: "Resolved", tone: "ok", icon: "check", sentence: recoveredSentence(environment) };
    case "failed":
      return { label: "Failed", tone: "bad", icon: "x", sentence: "The run ended without a verified recovery: the evidence stayed insufficient, or a person must decide." };
    case "error":
      return { label: "Error", tone: "bad", icon: "x", sentence: "The run failed with an error (a provider failure or a crash); the error is shown below." };
    case "cancelled":
      return { label: "Cancelled", tone: "neutral", icon: "stop", sentence: "Someone cancelled the run before it finished." };
    case "approval_rejected":
      return { label: "Blocked: fix rejected", tone: "warn", icon: "stop", sentence: "A person rejected the proposed fix, so it did not run." };
    case "approval_expired":
      return { label: "Blocked: approval expired", tone: "warn", icon: "clock", sentence: "Nobody decided before the approval request expired, so the fix did not run." };
    case "safety_blocked":
      return { label: "Blocked by safety", tone: "bad", icon: "shield", sentence: "The safety policy or the evidence gate refused the proposed fix." };
    case "budget_exhausted":
      return { label: "Budget exhausted", tone: "warn", icon: "stop", sentence: "A run limit was reached, so the run stopped safely." };
    case "quota_deferred":
      return { label: "Deferred: quota", tone: "warn", icon: "clock", sentence: "A free model's quota ran out: the run was deferred and nothing was decided." };
    case "in_progress":
      return { label: "In progress", tone: "accent", icon: "clock", sentence: "The run has not reached a final state yet." };
    default:
      return { label: NOT_RECORDED, tone: "neutral", icon: "minus", sentence: "The server did not report how this run ended." };
  }
}

/** The oracle's verdict (summary.oracle), or why there is none. */
export function oracleView(oracle: OracleVerdict | null | undefined): StatusView & { before: string; after: string; services: string[] } {
  if (!oracle) {
    return {
      label: "Not consulted",
      tone: "neutral",
      icon: "minus",
      detail: "No fix reached the oracle, so it was not asked.",
      before: "",
      after: "",
      services: [],
    };
  }
  const services = list(oracle.services_checked).map(text).filter(Boolean);
  const healthy = (value: unknown) => (value === true ? "healthy" : value === false ? "unhealthy" : NOT_RECORDED);
  return {
    label: oracle.verified ? "Recovery verified" : "Not recovered",
    tone: oracle.verified ? "ok" : "bad",
    icon: oracle.verified ? "check" : "x",
    detail: text(oracle.oracle) || NOT_RECORDED,
    before: healthy(oracle.healthy_before),
    after: healthy(oracle.healthy_after),
    services,
  };
}

/**
 * How long the oracle waited for the target after the fix (the verification.recovery event): a
 * simulated target is probed once; a live one every few seconds until healthy or its settle
 * window ends. "" when the trace did not record it.
 */
export function settleWords(payload: Record<string, unknown> | null | undefined, target: TargetFacts = targetFacts(null)): string {
  const p = record(payload);
  const probes = count(p.probes);
  const waited = count(p.waited_seconds);
  if (probes === null) return "";
  const live = p.simulated === false || (p.simulated !== true && target.state === "live");
  const where = p.simulated === true ? "simulated target" : live ? "live target" : "target";
  const times = `${probes} probe${probes === 1 ? "" : "s"}`;
  const over = waited !== null && probes > 1 ? ` over ${waited.toFixed(1)} s` : "";
  const window = live && target.settleSeconds !== null ? ` (settle window ${target.settleSeconds} s)` : "";
  return `${where} · ${times}${over}${window}`;
}

/** One side of a service's health the oracle observed (GroundTruthHealth: status, replicas, error rate). */
export interface ObservedHealth {
  status: string;
  ready: number | null;
  replicas: number | null;
  /** A ratio (0.3 = 30% of requests failed); null where the target measures no request errors. */
  errorRate: number | null;
}

export interface SignalRow {
  service: string;
  before: ObservedHealth | null;
  after: ObservedHealth | null;
  /** Status, readiness or error rate differ between before and after. */
  changed: boolean;
}

function observedHealth(value: unknown): ObservedHealth | null {
  const h = record(value);
  if (!Object.keys(h).length) return null;
  const rate = h.error_rate;
  return {
    status: text(h.status) || NOT_RECORDED,
    ready: count(h.ready_replicas),
    replicas: count(h.replicas),
    errorRate: typeof rate === "number" && Number.isFinite(rate) ? rate : null,
  };
}

/**
 * The oracle's own observations (RecoveryResult.observed_signals: {before, after}, each keyed by
 * service), one row per service in the order the oracle checked them, the services that changed
 * first. Ground truth from the environment's health check: RECAP's trusted channel, not an agent's.
 */
export function recoverySignals(observed: unknown): SignalRow[] {
  const signals = record(observed);
  const before = record(signals.before);
  const after = record(signals.after);
  const services = [...new Set([...Object.keys(after), ...Object.keys(before)])];
  const rows = services.map((service): SignalRow => {
    const b = observedHealth(before[service]);
    const a = observedHealth(after[service]);
    const changed = JSON.stringify(b) !== JSON.stringify(a);
    return { service, before: b, after: a, changed };
  });
  return [...rows.filter((r) => r.changed), ...rows.filter((r) => !r.changed)];
}

/** "Running · 2/2 ready · errors 30%" (a side the oracle did not observe reads NOT_RECORDED). */
export function healthWords(health: ObservedHealth | null): string {
  if (!health) return NOT_RECORDED;
  const ready = health.ready !== null && health.replicas !== null ? ` · ${health.ready}/${health.replicas} ready` : "";
  const errors = health.errorRate !== null ? ` · errors ${Number((health.errorRate * 100).toFixed(1))}%` : "";
  return `${health.status}${ready}${errors}`;
}

/** The pages that show more of one run, with what each shows (the lab links to them when a run ends). */
export const INSPECT_PAGES = [
  { page: "timeline", hint: "Each agent's tool calls and model requests on one timeline" },
  { page: "evidence", hint: "What was observed, claimed and verified" },
  { page: "knowledge", hint: "The runbooks the agents could search" },
  { page: "memory", hint: "Past incidents the run read, and why each was or was not reused" },
  { page: "safety", hint: "Policy verdicts and the approval log" },
] as const;

// ==================================================================================================
// The live feed: merging, resuming, and the bounded window
// ==================================================================================================

/**
 * `incoming` merged into `current` by `seq`: ascending, without duplicates. Returns `current`
 * itself when nothing new arrived (so React can skip a render).
 */
export function mergeBySeq<T extends { seq: number }>(current: readonly T[], incoming: readonly T[]): readonly T[] {
  if (!incoming.length) return current;
  let last = current.length ? current[current.length - 1].seq : -Infinity;
  let inOrder = true;
  for (const event of incoming) {
    if (!(event.seq > last)) {
      inOrder = false;
      break;
    }
    last = event.seq;
  }
  if (inOrder) return [...current, ...incoming];
  const bySeq = new Map<number, T>();
  for (const event of current) bySeq.set(event.seq, event);
  let added = 0;
  for (const event of incoming) {
    if (!bySeq.has(event.seq)) {
      bySeq.set(event.seq, event);
      added += 1;
    }
  }
  if (!added) return current;
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

/** How many times a broken live stream is reopened (resuming after the last event) before polling. */
export const MAX_STREAM_RESUMES = 3;

/** Wait before reopening the stream: 1 s, 2 s, 4 s; null: give up and poll instead. */
export function resumeDelay(attempt: number): number | null {
  if (!Number.isInteger(attempt) || attempt < 0 || attempt >= MAX_STREAM_RESUMES) return null;
  return 1000 * 2 ** attempt;
}

/** How events reach the lab: the SSE stream, a reconnect in progress, polling, or a recorded run. */
export type LabTransport = "idle" | "sse" | "reconnecting" | "polling" | "recorded";

/**
 * How events reach the lab, in words. Once the run has finished, the stream's job is done:
 * "Stream complete" with the number of events received (or "Stream stopped" when it ended on an
 * error), never "live".
 */
export function transportView(
  transport: LabTransport,
  resumes: number,
  lastSeq: number,
  finished = false,
  received = 0,
  failed = false,
): StatusView {
  if (finished && transport !== "recorded") {
    const count = `${received.toLocaleString("en-US")} event${received === 1 ? "" : "s"} received`;
    return failed
      ? { label: "Stream stopped", tone: "warn", icon: "alert", detail: count }
      : { label: "Stream complete", tone: "neutral", icon: "check", detail: resumes > 0 ? `${count} · reconnected ${resumes}×` : count };
  }
  switch (transport) {
    case "sse":
      return resumes > 0
        ? {
            label: "Live stream (SSE)",
            tone: "accent",
            icon: "zap",
            detail: `reconnected ${resumes}× · resumed after event #${lastSeq}`,
          }
        : { label: "Live stream (SSE)", tone: "accent", icon: "zap", detail: "" };
    case "reconnecting":
      return { label: "Reconnecting", tone: "warn", icon: "refresh", detail: lastSeq ? `will resume after event #${lastSeq}` : "" };
    case "polling":
      return { label: "Polling /events", tone: "warn", icon: "refresh", detail: "the live stream is unavailable" };
    case "recorded":
      return { label: "Recorded events", tone: "neutral", icon: "save", detail: "started by another process: no live updates here" };
    default:
      return { label: "Connecting", tone: "neutral", icon: "clock", detail: "" };
  }
}

/** Lines the feed renders at first, and adds per "Show earlier events". */
export const FEED_PAGE = 200;

/**
 * The first line the feed renders. Following the newest line, the window slides so that at most
 * `limit` lines render; paused (the reader scrolled up), it stays where it was, so nothing the
 * reader is looking at moves while new lines arrive below.
 */
export function feedStart(total: number, limit: number, frozenStart: number | null): number {
  const count = Math.max(0, Math.floor(total));
  if (frozenStart !== null && Number.isFinite(frozenStart)) return Math.min(Math.max(0, Math.floor(frozenStart)), count);
  return Math.max(0, count - Math.max(1, Math.floor(limit)));
}
