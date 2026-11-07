/**
 * What the Overview and Architecture & Learning pages show, from GET /system, GET /providers
 * and GET /runs (the typed views in ../dto):
 *
 *   - component, adapter and provider statuses as words, tones and icons (never color alone)
 *   - the active environment (the simulated shop, the Kind demo cluster or SREGym's cluster)
 *     and the answer of its readiness check (POST /environment/check)
 *   - run outcomes counted PER DATA SOURCE AND TARGET (scripted, replay, local LLM, free
 *     hosted LLM; the simulator or a real cluster), never added up into one number
 *   - active incidents, run durations, anything the server still lists as planned
 *   - the map of RECAP's flow: each step's code modules, ADRs, trace events, the /system
 *     components whose status it shows and how each target does it, and how a real
 *     Kubernetes action is authorized
 *
 * Every status and number comes from the server. A field the server did not record stays
 * null here ("Not recorded" on screen), never a default dressed as data. The flow map's
 * explanations are RECAP's own documentation (docs/flow.md, the ADRs), not measurements.
 *
 * This module imports types only, so the Node tests in ../../tests load it directly.
 */

import type { IconName } from "../components/Icon";
import type {
  AdapterStatus,
  ComponentState,
  ComponentStatus,
  FeatureFlags,
  ProviderOrigin,
  ProviderView,
  RunOutcome,
  RunStatus,
  RunSummary,
  SystemView,
} from "../dto";
import type { Tone } from "../format";
import type { PageId } from "../hooks";

/** A status as the page shows it: words first, then a tone and an icon that repeat them. */
export interface StatusLook {
  label: string;
  tone: Tone;
  icon: IconName;
  /** One plain sentence: what the status means. */
  title: string;
}

function words(text: string): string {
  const spaced = text.replace(/_/g, " ").trim();
  return spaced ? spaced.charAt(0).toUpperCase() + spaced.slice(1) : "Not reported";
}

// ======================================================================================
// Repository links (docs, ADRs, code)
// ======================================================================================

export const REPO_URL = "https://github.com/codeit-ronit/recap";

/** A repository path on GitHub (main branch). Folders end with "/". */
export function repoUrl(path: string): string {
  const clean = path.replace(/^\/+/, "");
  return `${REPO_URL}/${clean.endsWith("/") ? "tree" : "blob"}/main/${clean.replace(/\/+$/, "")}`;
}

/** The documents the two pages point newcomers to (repository paths). */
export const DOCS = {
  flow: "docs/flow.md",
  adrIndex: "docs/decisions/README.md",
  architecture: "docs/architecture.md",
  threatModel: "docs/threat-model.md",
  m5Plan: "docs/m5-integration-plan.md",
  m5Progress: "docs/m5-progress.md",
  kindTarget: "infra/kind/README.md",
  sregymTarget: "integrations/sregym/README.md",
  demoWalkthrough: "docs/demo-walkthrough.md",
  learningNotes: "docs/learning-notes.md",
} as const;

// ======================================================================================
// Components and environment adapters (GET /system)
// ======================================================================================

const COMPONENT_LOOKS: Record<ComponentState, StatusLook> = {
  active: { label: "Active", tone: "ok", icon: "check", title: "Wired in: runs use it now." },
  available: { label: "Available", tone: "info", icon: "dot", title: "Implemented, and used when a run asks for it." },
  unavailable: { label: "Unavailable", tone: "warn", icon: "alert", title: "Implemented, but it cannot run now (the detail says why)." },
  disabled: { label: "Disabled", tone: "neutral", icon: "minus", title: "Implemented and switched off by configuration." },
  planned: { label: "Planned", tone: "neutral", icon: "clock", title: "Not implemented in this repository: a later milestone." },
  error: { label: "Error", tone: "bad", icon: "x", title: "Configured, but it cannot be built (the detail says why)." },
};

/** A component's status (ComponentState) as words; an unknown state is shown as the server sent it. */
export function componentLook(state: ComponentState | string | null | undefined): StatusLook {
  if (state && state in COMPONENT_LOOKS) return COMPONENT_LOOKS[state as ComponentState];
  return { label: state ? words(state) : "Not reported", tone: "neutral", icon: "minus", title: "The server did not report a known status." };
}

/** An environment adapter's status: a planned one names its milestone and is never "active". */
export function adapterLook(adapter: Pick<AdapterStatus, "status" | "milestone">): StatusLook {
  const look = componentLook(adapter.status);
  if (adapter.status === "planned" && adapter.milestone) return { ...look, label: `Planned · ${adapter.milestone}` };
  return look;
}

/** Display names of the adapter kinds this repository knows of; anything else keeps its kind. */
export function adapterName(kind: string): string {
  switch (kind) {
    case "simulated":
      return "Simulated shop";
    case "kind":
      return "Kind (local Kubernetes)";
    case "sregym":
      return "SREGym";
    default:
      return kind;
  }
}

export interface ComponentTally {
  /** Count per status, in a fixed order, only the statuses that occur. */
  counts: { state: string; count: number }[];
  /** Every component that is not active (what a reader should look at). */
  notActive: ComponentStatus[];
}

const STATE_ORDER: readonly string[] = ["active", "available", "unavailable", "disabled", "planned", "error"];

export function tallyComponents(components: readonly ComponentStatus[]): ComponentTally {
  const counts = new Map<string, number>();
  for (const c of components) counts.set(c.status, (counts.get(c.status) ?? 0) + 1);
  const known = STATE_ORDER.filter((s) => counts.has(s));
  const other = [...counts.keys()].filter((s) => !STATE_ORDER.includes(s));
  return {
    counts: [...known, ...other].map((state) => ({ state, count: counts.get(state) ?? 0 })),
    notActive: components.filter((c) => c.status !== "active"),
  };
}

/** A component by id, or null when this server does not list it. */
export function componentById(system: Pick<SystemView, "components"> | null | undefined, id: string): ComponentStatus | null {
  return system?.components.find((c) => c.id === id) ?? null;
}

// ======================================================================================
// The active environment (GET /system) and its check (POST /environment/check)
// ======================================================================================

/** The adapter kinds this repository implements (EnvironmentInfo.kind), in reading order. */
export const TARGET_KINDS = ["simulated", "kind", "sregym"] as const;
export type TargetKind = (typeof TARGET_KINDS)[number];

/** The adapters that act on a real cluster (recap/runtime.py LIVE_ADAPTERS). */
const LIVE_KINDS: ReadonlySet<string> = new Set(["kind", "sregym"]);

/** The configured adapter's entry in GET /system, or null when the server lists none. */
export function configuredAdapter(system: Pick<SystemView, "environment_adapters">): AdapterStatus | null {
  return system.environment_adapters.find((adapter) => adapter.configured) ?? null;
}

/**
 * Whether an adapter runs this server's incidents (AdapterStatus.operational: only the
 * configured one does). GET /system never contacts a cluster, so for a real cluster this
 * says where runs go, not that the cluster answers: POST /environment/check asks it.
 */
export function operationalLook(adapter: Pick<AdapterStatus, "operational" | "kind">): StatusLook {
  if (!adapter.operational) {
    return { label: "Not operational", tone: "neutral", icon: "minus", title: "This server's runs do not use it: every run uses the configured adapter." };
  }
  return LIVE_KINDS.has(adapter.kind)
    ? {
        label: "Operational",
        tone: "ok",
        icon: "check",
        title: "Every run on this server uses it. GET /system does not contact the cluster: Check now (Overview, Active environment) does.",
      }
    : { label: "Operational", tone: "ok", icon: "check", title: "Every run on this server uses it." };
}

/** Where a real-cluster target is set up and explained; null for the simulator and unknown kinds. */
export function targetGuide(kind: string | null | undefined): { path: string; title: string } | null {
  switch (kind) {
    case "kind":
      return { path: DOCS.kindTarget, title: "How the Kind demo cluster, its identities and its limits are set up" };
    case "sregym":
      return { path: DOCS.sregymTarget, title: "How RECAP works a SREGym problem on SREGym's own cluster" };
    default:
      return null;
  }
}

/** The answer of POST /environment/check, or one of its checks, as words: never color alone. */
export function checkLook(ok: boolean): StatusLook {
  return ok
    ? { label: "Passed", tone: "ok", icon: "check", title: "The server reached the target as a run would. Nothing was changed." }
    : { label: "Failed", tone: "bad", icon: "x", title: "The server could not reach the target as a run would (the detail says why). Nothing was changed." };
}

// ======================================================================================
// Planned items: what the server still lists as not built
// ======================================================================================

export interface Gap {
  id: string;
  title: string;
  /** The server's own words. */
  detail: string;
  look: StatusLook;
  /** Where the fact comes from (a GET /system field). */
  source: string;
}

/**
 * Everything the server still lists as not built: planned environment adapters, planned
 * components and planned evaluation modes. Since M5 a server of this repository lists none
 * (Kind and SREGym are implemented), so the Overview shows the list only when it is not
 * empty; an empty list means the server reports no planned item, nothing more.
 */
export function plannedGaps(system: Pick<SystemView, "environment_adapters" | "components" | "features">): Gap[] {
  const gaps: Gap[] = [];
  for (const adapter of system.environment_adapters) {
    if (adapter.status !== "planned") continue;
    gaps.push({
      id: `adapter:${adapter.kind}`,
      title: `${adapterName(adapter.kind)} adapter`,
      detail: adapter.detail,
      look: adapterLook(adapter),
      source: "environment_adapters",
    });
  }
  for (const component of system.components) {
    if (component.status !== "planned") continue;
    gaps.push({
      id: `component:${component.id}`,
      title: `${component.interface}: ${component.implementation}`,
      detail: component.detail,
      look: componentLook("planned"),
      source: "components",
    });
  }
  for (const [mode, status] of Object.entries(system.features.evaluation_modes ?? {})) {
    if (status !== "planned") continue;
    gaps.push({
      id: `eval:${mode}`,
      title: `Evaluation mode ${mode}`,
      detail: "Listed as planned: this server cannot run or record it.",
      look: componentLook("planned"),
      source: "features.evaluation_modes",
    });
  }
  return gaps;
}

// ======================================================================================
// Providers (GET /providers)
// ======================================================================================

/** ProviderView.status (ready, needs_setup, stopped, disabled, unavailable) as words. */
export function providerReadiness(status: string | null | undefined): StatusLook {
  switch (status) {
    case "ready":
      return { label: "Ready", tone: "ok", icon: "check", title: "It could run now." };
    case "needs_setup":
      return { label: "Needs setup", tone: "warn", icon: "alert", title: "Something is missing before it can run (the detail says what)." };
    case "stopped":
      return { label: "Stopped", tone: "bad", icon: "stop", title: "RECAP stopped it (for example, a free quota ran out)." };
    case "disabled":
      return { label: "Disabled", tone: "neutral", icon: "minus", title: "Switched off for this project." };
    case "unavailable":
      return { label: "Unavailable", tone: "warn", icon: "alert", title: "It cannot run now (the detail says why)." };
    default:
      return { label: status ? words(status) : "Not reported", tone: "neutral", icon: "minus", title: "The server did not report a known status." };
  }
}

/** Whether a client may pick the provider for one run (ProviderView.selectable). */
export function selectableLook(selectable: boolean): StatusLook {
  return selectable
    ? { label: "Selectable per run", tone: "ok", icon: "check", title: "A client may pick it when starting one run." }
    : { label: "Not selectable", tone: "neutral", icon: "lock", title: "A client cannot pick it for a run." };
}

/** What the page may say about a provider's API key: present or missing, never its value. */
export function keyStatus(provider: Pick<ProviderView, "key_required" | "key_present">): string | null {
  if (!provider.key_required) return null;
  if (provider.key_present === true) return "API key set (its value is never shown)";
  if (provider.key_present === false) return "API key missing";
  return "API key not checked";
}

const ORIGINS: readonly ProviderOrigin[] = ["scripted", "local_llm", "free_hosted_llm", "paid_llm", "replay"];

/** The fields of GET /system `provider` (an untyped record) a data-source badge reads; anything else is dropped. */
export interface ProviderSource {
  name: string | null;
  origin?: ProviderOrigin;
  kind?: "scripted" | "llm" | "replay";
  model: string | null;
  /** Why the default provider cannot be built, when the server says so. */
  error: string | null;
}

export function providerSource(provider: Record<string, unknown> | null | undefined): ProviderSource {
  const p = provider ?? {};
  const text = (value: unknown) => (typeof value === "string" && value.trim() ? value : null);
  const origin = ORIGINS.find((o) => o === p.origin);
  const kind = p.kind === "scripted" || p.kind === "llm" || p.kind === "replay" ? p.kind : undefined;
  return { name: text(p.name), origin, kind, model: text(p.model), error: text(p.error) };
}

/** Providers in the order a reader compares them: the default first, then by class. */
export function orderProviders<T extends Pick<ProviderView, "default" | "provider_class" | "name">>(providers: readonly T[]): T[] {
  const rank = (p: T) => SOURCE_ORDER.indexOf(p.provider_class);
  return [...providers].sort((a, b) => Number(b.default) - Number(a.default) || rank(a) - rank(b) || a.name.localeCompare(b.name));
}

// ======================================================================================
// Runs (GET /runs): data source, outcome, duration, liveness
// ======================================================================================

/** Where a run's decisions came from: the provider class, or "unknown" when not recorded. */
export type SourceKey = ProviderOrigin | "unknown";

/** Column order of the outcome breakdown. */
export const SOURCE_ORDER: readonly SourceKey[] = ["scripted", "replay", "local_llm", "free_hosted_llm", "paid_llm", "unknown"];

/** Sources the breakdown always shows (with "no runs yet" when empty); the others only when present. */
export const ALWAYS_SHOWN: ReadonlySet<SourceKey> = new Set<SourceKey>(["scripted", "replay", "local_llm", "free_hosted_llm"]);

/** Short names of the sources (the badges say the same in capitals). */
export const SOURCE_LABELS: Readonly<Record<SourceKey, string>> = {
  scripted: "Scripted baseline",
  replay: "Replay",
  local_llm: "Local LLM",
  free_hosted_llm: "Free hosted LLM",
  paid_llm: "Paid LLM",
  unknown: "Source not recorded",
};

/** What each source means, in one plain sentence (RECAP's documentation). */
export const SOURCE_NOTES: Readonly<Record<SourceKey, string>> = {
  scripted: "Fixed playbooks, not an LLM: the regression baseline.",
  replay: "Recorded answers of an earlier real-model run, replayed offline: no model is called now.",
  local_llm: "New inference by a self-hosted open-weight model (Ollama), on this machine or on the owner's own VM. No paid API.",
  free_hosted_llm: "New inference on a free hosted plan, behind RECAP's free-tier guard.",
  paid_llm: "A billed API. This project keeps it disabled.",
  unknown: "Runs whose decision source was not recorded (recorded before provider classes existed).",
};

/** The run's data source, read only from what the server recorded (as DataSourceBadge does). */
export function sourceOf(run: Pick<RunSummary, "provider_class" | "provider_kind">): SourceKey {
  if (run.provider_class) return run.provider_class;
  if (run.provider_kind === "scripted" || run.provider_kind === "replay") return run.provider_kind;
  return "unknown";
}

/** Outcome order: verified recoveries first, then the ways a run stops, then runs in progress. */
export const OUTCOME_ORDER: readonly RunOutcome[] = [
  "resolved",
  "failed",
  "error",
  "safety_blocked",
  "approval_rejected",
  "approval_expired",
  "budget_exhausted",
  "quota_deferred",
  "cancelled",
  "in_progress",
];

const OUTCOME_LOOKS: Record<RunOutcome, StatusLook> = {
  resolved: { label: "Resolved", tone: "ok", icon: "check", title: "The independent recovery oracle verified the recovery." },
  failed: { label: "Failed", tone: "bad", icon: "x", title: "The evidence stayed insufficient, or a person had to decide." },
  error: { label: "Error", tone: "bad", icon: "alert", title: "The run failed with an error (a provider failure or a crash)." },
  safety_blocked: { label: "Safety blocked", tone: "warn", icon: "shield", title: "The safety policy or the evidence gate refused the fix." },
  approval_rejected: { label: "Approval rejected", tone: "warn", icon: "x", title: "A person rejected the proposed fix." },
  approval_expired: { label: "Approval expired", tone: "warn", icon: "clock", title: "Nobody decided before the approval request expired." },
  budget_exhausted: { label: "Budget exhausted", tone: "warn", icon: "stop", title: "A run limit stopped the run safely." },
  quota_deferred: { label: "Quota deferred", tone: "warn", icon: "clock", title: "A free model's quota ran out: the run was deferred, nothing decided." },
  cancelled: { label: "Cancelled", tone: "neutral", icon: "stop", title: "Someone cancelled the run." },
  in_progress: { label: "In progress", tone: "accent", icon: "live", title: "The run has not reached a final state." },
};

/** A run outcome (RunSummary.outcome) as words; an unknown value is shown as sent. */
export function outcomeLook(outcome: RunOutcome | string | null | undefined): StatusLook {
  if (outcome && outcome in OUTCOME_LOOKS) return OUTCOME_LOOKS[outcome as RunOutcome];
  return { label: outcome ? words(outcome) : "Not recorded", tone: "neutral", icon: "minus", title: "The server did not report a known outcome." };
}

export interface OutcomeCount {
  outcome: string;
  count: number;
}

/** Where a run's actions went, read only from what the run recorded (RunSummary). */
export interface RunTarget {
  /** What the breakdown groups by: "simulated", "live:<kind>", or "unknown" (not recorded). */
  key: string;
  /** RunSummary.environment_simulated; null when the run did not record it. */
  simulated: boolean | null;
  /** RunSummary.environment_kind; null when the run did not record it. */
  kind: string | null;
}

/** The target of a run: the simulator, a real cluster of one kind, or not recorded (never a guess). */
export function targetOf(run: Pick<RunSummary, "environment_simulated" | "environment_kind">): RunTarget {
  const kind = typeof run.environment_kind === "string" && run.environment_kind.trim() ? run.environment_kind.trim() : null;
  if (run.environment_simulated === true) return { key: "simulated", simulated: true, kind };
  if (run.environment_simulated === false) return { key: `live:${kind ?? ""}`, simulated: false, kind };
  return { key: "unknown", simulated: null, kind: null };
}

/** A target in words: "Simulated target", "Live target · Kind (local Kubernetes)", "Target not recorded". */
export function targetLabel(target: Pick<RunTarget, "simulated" | "kind">): string {
  if (target.simulated === true) return "Simulated target";
  if (target.simulated === false) return `Live target · ${target.kind ? adapterName(target.kind) : "kind not reported"}`;
  return "Target not recorded";
}

/** What a column's target adds to its source's note; null for the simulator (the usual case). */
export function targetNote(target: Pick<RunTarget, "simulated" | "kind"> | null): string | null {
  if (!target || target.simulated === true) return null;
  if (target.simulated === null) return "These runs did not record which environment they ran against.";
  if (target.kind === "sregym") {
    return "These runs acted on SREGym's cluster, not a simulation. Resolved is RECAP's own oracle; SREGym's grader gives the benchmark's verdict separately (Evaluations).";
  }
  return LIVE_KINDS.has(target.kind ?? "")
    ? "These runs acted on a real cluster, not a simulation."
    : "These runs acted on a live target, not a simulation.";
}

/** Simulated first, then each real cluster by kind, then runs that recorded no target. */
function targetRank(target: RunTarget | null): [number, string] {
  if (!target || target.simulated === true) return [0, ""];
  return target.simulated === false ? [1, target.kind ?? ""] : [2, ""];
}

export interface SourceBreakdown {
  source: SourceKey;
  /** Where these runs acted; null for a source shown with no run at all (no target to name). */
  target: RunTarget | null;
  total: number;
  /** Outcomes that occur, in OUTCOME_ORDER (unknown outcomes last). */
  outcomes: OutcomeCount[];
  resolved: number;
  inProgress: number;
  /** Distinct scenarios among these runs. */
  scenarios: number;
  /** Replays only: distinct recordings replayed (null when no replay names its recording). */
  recordings: number | null;
}

/**
 * Run outcomes counted separately for every data source AND target. Nothing is added across
 * them: a scripted run, a replay of a recording and a new model run are different kinds of
 * evidence, and a run on the simulator is not a run on a real cluster. Sources in
 * ALWAYS_SHOWN appear even with no runs (one empty column, no target); the others, and each
 * source's targets, only when present.
 */
type CountedRun = Pick<RunSummary, "provider_class" | "provider_kind" | "outcome" | "scenario_id" | "replay" | "environment_simulated" | "environment_kind">;

export function outcomesBySource(runs: readonly CountedRun[]): SourceBreakdown[] {
  const groups = new Map<string, { source: SourceKey; target: RunTarget | null; runs: CountedRun[] }>();
  for (const run of runs) {
    const source = sourceOf(run);
    const target = targetOf(run);
    const key = `${source}|${target.key}`;
    const group = groups.get(key) ?? { source, target, runs: [] };
    group.runs.push(run);
    groups.set(key, group);
  }
  const present = new Set([...groups.values()].map((group) => group.source));
  for (const source of SOURCE_ORDER) {
    if (ALWAYS_SHOWN.has(source) && !present.has(source)) groups.set(`${source}|`, { source, target: null, runs: [] });
  }
  const ordered = [...groups.values()].sort((a, b) => {
    const [rankA, kindA] = targetRank(a.target);
    const [rankB, kindB] = targetRank(b.target);
    return SOURCE_ORDER.indexOf(a.source) - SOURCE_ORDER.indexOf(b.source) || rankA - rankB || kindA.localeCompare(kindB);
  });
  return ordered.map(({ source, target, runs: list }) => {
    const counts = new Map<string, number>();
    for (const run of list) counts.set(run.outcome, (counts.get(run.outcome) ?? 0) + 1);
    const known = OUTCOME_ORDER.filter((o) => counts.has(o));
    const other = [...counts.keys()].filter((o) => !(OUTCOME_ORDER as readonly string[]).includes(o)).sort();
    let recordings: number | null = null;
    if (source === "replay") {
      const ids = new Set(list.map((run) => run.replay?.recording_id ?? run.replay?.fixture_id ?? null).filter((id): id is string => !!id));
      recordings = ids.size ? ids.size : null;
    }
    return {
      source,
      target,
      total: list.length,
      outcomes: [...known, ...other].map((outcome) => ({ outcome, count: counts.get(outcome) ?? 0 })),
      resolved: counts.get("resolved") ?? 0,
      inProgress: counts.get("in_progress") ?? 0,
      scenarios: new Set(list.map((run) => run.scenario_id)).size,
      recordings,
    };
  });
}

/** How long a run took: measured, still running, or not recorded (never a guess). */
export type RunDuration = { kind: "measured"; ms: number } | { kind: "running" } | { kind: "not_recorded" };

export function runDuration(run: Pick<RunSummary, "wall_seconds" | "terminal">): RunDuration {
  if (typeof run.wall_seconds === "number" && Number.isFinite(run.wall_seconds) && run.wall_seconds >= 0) {
    return { kind: "measured", ms: run.wall_seconds * 1000 };
  }
  return run.terminal ? { kind: "not_recorded" } : { kind: "running" };
}

/** Runs that have not reached a final state, in the order given (GET /runs: newest first). */
export function activeRuns<T extends Pick<RunSummary, "terminal">>(runs: readonly T[]): T[] {
  return runs.filter((run) => !run.terminal);
}

/** The Overview's at-a-glance groups of how runs ended (RunSummary.outcome), across every data source. */
export type GlanceKey = "resolved" | "blocked" | "failed" | "active" | "other";

export const GLANCE_GROUPS: readonly { key: GlanceKey; label: string; tone: Tone; hint: string }[] = [
  { key: "resolved", label: "Resolved", tone: "ok", hint: "The independent oracle verified the recovery." },
  { key: "blocked", label: "Blocked", tone: "warn", hint: "The safety policy, the evidence gate or a person stopped the fix, or its approval expired." },
  { key: "failed", label: "Failed", tone: "bad", hint: "No verified recovery: the evidence stayed insufficient, an error, or a run limit." },
  { key: "active", label: "Active", tone: "accent", hint: "Not finished yet." },
  { key: "other", label: "Cancelled or deferred", tone: "neutral", hint: "Someone cancelled the run, or a free model's quota deferred it." },
];

const GLANCE_OF: Readonly<Record<string, GlanceKey>> = {
  resolved: "resolved",
  safety_blocked: "blocked",
  approval_rejected: "blocked",
  approval_expired: "blocked",
  failed: "failed",
  error: "failed",
  budget_exhausted: "failed",
  in_progress: "active",
};

/** How many runs fall in each at-a-glance group; an unfinished run is active whatever it reports. */
export function glanceCounts(runs: readonly Pick<RunSummary, "outcome" | "terminal">[]): Record<GlanceKey, number> {
  const counts: Record<GlanceKey, number> = { resolved: 0, blocked: 0, failed: 0, active: 0, other: 0 };
  for (const run of runs) counts[run.terminal ? (GLANCE_OF[run.outcome] ?? "other") : "active"] += 1;
  return counts;
}

/** Whether THIS server can follow an unfinished run live (RunSummary.status). */
export function livenessLook(status: RunStatus | string): StatusLook {
  switch (status) {
    case "running":
      return { label: "Running in this server", tone: "accent", icon: "live", title: "This API server runs it: its events stream live." };
    case "external":
      return {
        label: "Not running in this server",
        tone: "neutral",
        icon: "minus",
        title: "Started by another process (the CLI or an earlier server) and not finished: its live events exist only there.",
      };
    case "error":
      return { label: "Run task failed", tone: "bad", icon: "x", title: "The server's task for this run ended with an error." };
    default:
      return { label: "Status unknown", tone: "neutral", icon: "minus", title: "The server cannot tell whether this run is still going." };
  }
}

/** How a scan of the newest runs relates to all of them (X-Total-Count), in words. */
export function scanNote(counted: number, total: number | null): string | null {
  if (total === null) return null;
  if (total > counted) return `Counted over the newest ${counted.toLocaleString("en-US")} of ${total.toLocaleString("en-US")} runs.`;
  return null;
}

// ======================================================================================
// Access (GET /system features, the operator session)
// ======================================================================================

/** The sign-in state of this tab: never the token itself. */
export type SessionState =
  | { kind: "signed_out" }
  | { kind: "checking" }
  | { kind: "signed_in"; operator: string }
  | { kind: "rejected" }
  | { kind: "unverified"; message: string };

export function sessionLook(state: SessionState): StatusLook {
  switch (state.kind) {
    case "signed_in":
      return { label: `Signed in as ${state.operator}`, tone: "ok", icon: "key", title: "This tab holds an operator token the API accepted." };
    case "checking":
      return { label: "Checking the sign-in", tone: "neutral", icon: "clock", title: "Asking the API whether this tab's token is still valid." };
    case "rejected":
      return { label: "Token rejected", tone: "warn", icon: "alert", title: "The API no longer accepts this tab's token: sign in again." };
    case "unverified":
      return { label: "Sign-in not verified", tone: "warn", icon: "alert", title: state.message };
    default:
      return { label: "Signed out", tone: "neutral", icon: "lock", title: "Read-only: this tab holds no operator token." };
  }
}

/** The actions that need the operator token (from the API's own rules). */
export const OPERATOR_ACTIONS = ["start a run or a replay", "approve or reject a fix", "cancel a run", "start an evaluation"] as const;

export function accessFacts(features: Pick<FeatureFlags, "reads_require_token" | "writes_require_operator_token">): { reads: StatusLook; writes: StatusLook } {
  return {
    reads: features.reads_require_token
      ? { label: "Token required", tone: "warn", icon: "lock", title: "This server asks for a token on reads." }
      : { label: "Open", tone: "ok", icon: "check", title: "Every read-only view works without a token." },
    writes: features.writes_require_operator_token
      ? { label: "Operator token required", tone: "info", icon: "key", title: "The API refuses state-changing calls without the operator token." }
      : { label: "No token required", tone: "bad", icon: "alert", title: "This server accepts state-changing calls without a token." },
  };
}

// ======================================================================================
// The flow map (Architecture & Learning)
// ======================================================================================

export type ArchNodeId =
  | "alert"
  | "controller"
  | "retrieval"
  | "routing"
  | "specialists"
  | "gateway"
  | "verifier"
  | "approval"
  | "write"
  | "oracle"
  | "episode"
  | "environment"
  | "telemetry";

/** Who or what a step's content comes from: trusted code, untrusted data, read-only agents, a person. */
export type Trust = "trusted" | "untrusted" | "readonly" | "human";

export const TRUST_LABELS: Readonly<Record<Trust, string>> = {
  trusted: "trusted code",
  untrusted: "untrusted data",
  readonly: "read-only agents",
  human: "human decision",
};

export interface Adr {
  id: string;
  title: string;
  file: string;
}

const ADR_LIST: readonly Adr[] = [
  { id: "0001", title: "Build V1 as a modular monolith", file: "0001-modular-monolith.md" },
  { id: "0002", title: "Store everything in local SQLite files", file: "0002-sqlite-storage.md" },
  { id: "0003", title: "A scripted, deterministic policy is the default provider", file: "0003-deterministic-default-provider.md" },
  { id: "0004", title: "Reach the environment through a real MCP server over stdio", file: "0004-real-mcp-over-stdio.md" },
  { id: "0005", title: "One central safety gateway, and the controller as the only executor", file: "0005-central-safety-gateway-and-executor.md" },
  { id: "0006", title: "Use a deterministic simulated environment for V1", file: "0006-simulated-environment-for-v1.md" },
  { id: "0007", title: "Make the target environment a replaceable adapter", file: "0007-replaceable-environment-adapter.md" },
  { id: "0010", title: "Label-free views for providers, and a yes/no oracle", file: "0010-label-free-views-and-a-yes-no-oracle.md" },
  { id: "0012", title: "Platform signals outrank log text in the evidence verifier", file: "0012-platform-signals-outrank-log-text.md" },
  { id: "0013", title: "Stabilization epochs, and a deadline on every provider call", file: "0013-stabilization-epochs-and-deadlines.md" },
  { id: "0014", title: "The write boundary verifies signed, single-use capabilities", file: "0014-signed-capabilities-at-the-write-boundary.md" },
  { id: "0015", title: "Operator authentication, and approvals where \"no\" wins", file: "0015-operator-auth-and-no-wins-approvals.md" },
  { id: "0016", title: "Zero-cost model providers and a constrained step protocol", file: "0016-zero-cost-llm-providers-and-a-constrained-step-protocol.md" },
  { id: "0018", title: "Record every model exchange, and replay real runs offline in CI", file: "0018-record-every-model-exchange-and-replay-in-ci.md" },
  { id: "0020", title: "Independent specialists with role contracts", file: "0020-independent-specialists-with-role-contracts.md" },
  { id: "0021", title: "Evidence rules settle disagreements, never votes", file: "0021-evidence-rules-settle-disagreements-never-votes.md" },
  { id: "0022", title: "Hybrid retrieval with a local embedder, and agent search", file: "0022-hybrid-retrieval-with-a-local-embedder-and-agent-search.md" },
  { id: "0023", title: "Memory partitions by origin, and evidence-checked reuse", file: "0023-memory-partitions-by-origin-and-evidence-checked-reuse.md" },
  { id: "0027", title: "A typed, read-only API over the run records", file: "0027-a-typed-read-only-api-over-the-run-records.md" },
  { id: "0028", title: "Content-addressed recordings, replayed by manifest id", file: "0028-content-addressed-recordings-and-replay-by-manifest-id.md" },
  {
    id: "0029",
    title: "One evaluation record per run, modes that never share a number, and a named reason for every run that did not recover",
    file: "0029-one-evaluation-record-per-run-and-modes-that-never-share-a-number.md",
  },
  { id: "0030", title: "Mission Control shows only typed server facts, one screen per question", file: "0030-mission-control-reads-only-typed-server-facts.md" },
  {
    id: "0031",
    title: "RECAP on an external benchmark's cluster: the harness owns the fault, a release is an image tag",
    file: "0031-an-external-benchmark-harness-release-tags-and-pod-failure-onsets.md",
  },
];

export const ADRS: ReadonlyMap<string, Adr> = new Map(ADR_LIST.map((adr) => [adr.id, adr]));

export function adrPath(adr: Adr): string {
  return `docs/decisions/${adr.file}`;
}

export interface ArchPart {
  /** A short name (an agent role, a sub-system). */
  label: string;
  /** The /system component whose status it shows, if any. */
  component?: string;
  note?: string;
}

export interface ArchNode {
  id: ArchNodeId;
  /** Position in the flow (1-11); null for the target environment and cross-cutting telemetry. */
  step: number | null;
  title: string;
  /** One line under the title. */
  summary: string;
  icon: IconName;
  trust: Trust;
  /** GET /system component ids; the first is the step's own status. Empty: /system lists none. */
  components: string[];
  /** Sub-parts drawn inside the node (agents, search modes, ...). */
  parts: ArchPart[];
  /** What it does, in short paragraphs (RECAP's documentation). */
  what: string[];
  /** How each target does it, where the targets differ (RECAP's documentation). */
  onTarget?: Partial<Readonly<Record<TargetKind, string>>>;
  /** Repository paths of its code (folders end with "/"). */
  modules: string[];
  adrs: string[];
  /** Trace event categories it emits (recap/telemetry/tracer.py TraceCategory). */
  events: string[];
  /** The dashboard page where a run shows this step. */
  page: PageId | null;
  /** Label on the arrow to the next step. */
  next?: string;
  /** A loop back to an earlier step. */
  loop?: string;
}

export const ARCH_NODES: readonly ArchNode[] = [
  {
    id: "alert",
    step: 1,
    title: "Alert",
    summary: "The incident's starting signal: an affected service and its symptoms",
    icon: "flag",
    trust: "untrusted",
    components: ["environment"],
    parts: [],
    what: [
      "A run starts when the target environment has its scenario's problem in place and reports an alert: the affected service and its symptoms, as measured.",
      "The alert's words are data, never instructions: a model sees them only inside an untrusted-data wrapper. A scenario's evaluation labels (its failure family and expected fix) never leave the evaluation code.",
    ],
    onTarget: {
      simulated: "RECAP resets the simulated shop to the scenario, which raises its alert at once.",
      kind: "RECAP's reset restores the demo app's baseline, applies the scenario's fault steps as its own scenario identity, then waits for the alert it measures: probe errors, replicas not ready, a crash loop or a stuck rollout.",
      sregym: "SREGym's own harness deploys the app and injects the problem before the run. RECAP's reset changes nothing: it waits for the alert from the cluster's own state.",
    },
    modules: [
      "recap/core/interfaces.py",
      "recap/environments/simulated/",
      "fixtures/scenarios/",
      "recap/environments/kubernetes/scenario.py",
      "fixtures/kind-scenarios/",
      "fixtures/sregym-scenarios/",
    ],
    adrs: ["0006", "0007", "0010", "0031"],
    events: ["state.transition"],
    page: "live",
    next: "typed Incident",
  },
  {
    id: "controller",
    step: 2,
    title: "Incident controller",
    summary: "The lifecycle state machine, and the only component that executes a fix",
    icon: "compass",
    trust: "trusted",
    components: [],
    parts: [],
    what: [
      "Moves every run through CREATED, RETRIEVING, INVESTIGATING, PROPOSING, AWAITING_APPROVAL, MITIGATING and VERIFYING to a final state, saving a checkpoint after each transition.",
      "It enforces the run's hard budgets (steps, tool calls, estimated tokens, the tokens a model server reports, and the run clock) and is the only component that can execute a fix: agents only read.",
    ],
    modules: ["recap/harness/controller.py", "recap/core/lifecycle.py", "recap/harness/budget.py", "recap/runtime.py"],
    adrs: ["0001", "0005", "0013"],
    events: ["state.transition", "budget.exhausted", "run.summary"],
    page: "live",
  },
  {
    id: "retrieval",
    step: 3,
    title: "Retrieval & memory",
    summary: "Runbook search and similar past incidents, as untrusted context",
    icon: "book",
    trust: "untrusted",
    components: ["retriever", "hybrid_retrieval", "memory"],
    parts: [
      { label: "Runbook search", component: "retriever" },
      { label: "Hybrid search", component: "hybrid_retrieval" },
      { label: "Experience memory", component: "memory" },
    ],
    what: [
      "Before investigating, RECAP searches the runbooks for the alert (BM25 keywords by default; hybrid search adds a local embedding model fused by reciprocal rank) and its experience memory for similar past incidents. Agents can also search the runbooks themselves with a budgeted search_runbooks tool.",
      "Runbook text and past episodes are untrusted data. A similar episode is never reused on similarity alone: live evidence must show the same mechanism on the same service, with a fix that still fits.",
    ],
    modules: ["recap/knowledge/retriever.py", "recap/knowledge/bm25.py", "recap/knowledge/ollama_embedder.py", "recap/memory/store.py", "recap/memory/similarity.py"],
    adrs: ["0022", "0023"],
    events: ["retrieval.docs", "memory.search", "memory.assessment"],
    page: "knowledge",
  },
  {
    id: "routing",
    step: 4,
    title: "Adaptive router",
    summary: "One investigator, or two specialists in parallel",
    icon: "route",
    trust: "trusted",
    components: ["router"],
    parts: [
      { label: "single", note: "one investigator" },
      { label: "parallel", note: "two specialists" },
    ],
    what: [
      "Inspectable rules, first match wins: a single investigator for a clear single-service incident, two specialists in parallel when the signals say the incident is hard (several services affected, conflicting memories, ambiguous symptoms, an earlier fix that failed), and single again when the budget is too small for two.",
      "The rule id and its reason are traced for every round. A disagreement no evidence rule settles gets one follow-up round; if it stays open, a person decides.",
    ],
    modules: ["recap/routing/router.py", "recap/routing/signals.py", "recap/routing/orchestrator.py"],
    adrs: ["0020", "0021"],
    events: ["routing.decision", "investigation.round"],
    page: "timeline",
    next: "asyncio.gather when parallel",
  },
  {
    id: "specialists",
    step: 5,
    title: "Investigation agents",
    summary: "Read-only agents; their decisions come from the run's provider",
    icon: "users",
    trust: "readonly",
    components: ["provider"],
    parts: [
      { label: "investigator", note: "single" },
      { label: "telemetry_investigator", note: "parallel" },
      { label: "infrastructure_investigator", note: "parallel" },
    ],
    what: [
      "Each agent runs observe, reason, act steps with read-only tools. Its decisions come from the run's provider: the scripted baseline (not an LLM), a local model through Ollama, a free hosted model, or a replay of a recorded real-model run.",
      "In a parallel round each specialist sees the evidence from before the round plus its own, never the other's, and may only conclude what its own tools can observe. Every hypothesis must cite evidence ids.",
    ],
    modules: ["recap/harness/loop.py", "recap/agents/roles.py", "recap/context/packer.py", "recap/providers/"],
    adrs: ["0003", "0016", "0018", "0020"],
    events: ["agent.start", "context.pack", "agent.decision", "llm.call", "agent.finish"],
    page: "timeline",
    next: "read-only tool calls",
  },
  {
    id: "gateway",
    step: 6,
    title: "MCP tool gateway",
    summary: "Every tool call is judged by the safety policy, then sent over MCP",
    icon: "tool",
    trust: "trusted",
    components: ["tool_transport", "safety_policy"],
    parts: [
      { label: "MCP over stdio", component: "tool_transport" },
      { label: "Safety policy", component: "safety_policy" },
    ],
    what: [
      "A real MCP client and server (the official Python SDK), one server subprocess per run. The gateway validates typed arguments and applies the safety policy to every call from trusted configuration: ALLOW, APPROVAL_REQUIRED or DENY. A tool's own description of itself is never trusted.",
      "Each result is bounded, redacted, scanned for injection-like text and stored as untrusted evidence with an id the agents must cite.",
    ],
    modules: ["recap/tools/gateway.py", "recap/tools/mcp_client.py", "recap/tools/mcp_server.py", "recap/safety/policy.py", "recap/safety/injection.py"],
    adrs: ["0004", "0005"],
    events: ["tool.discovery", "policy.decision", "tool.call", "tool.result", "safety.injection_flag"],
    page: "safety",
    next: "evidence with ids",
  },
  {
    id: "verifier",
    step: 7,
    title: "Evidence verifier",
    summary: "Evidence rules decide which claims are supported; agents never vote",
    icon: "evidence",
    trust: "trusted",
    components: ["verifier"],
    parts: [],
    what: [
      "The merger pools the agents' claims, rules V1 to V7 judge each one against the evidence registry (platform signals outrank log text), and rules VP1 to VP4 later check the proposed fix.",
      "Disagreements are settled by evidence rules, never by votes. The verifier's open questions and leads point the next round at what nothing explains yet.",
    ],
    modules: ["recap/agents/verifier.py", "recap/agents/merger.py"],
    adrs: ["0012", "0021"],
    events: ["verification.merge", "verification.evidence"],
    page: "evidence",
    next: "best supported claim, one proposed fix",
    loop: "An open disagreement sends one question back to the router (step 4).",
  },
  {
    id: "approval",
    step: 8,
    title: "Human approval",
    summary: "A person approves one exact action; \"no\" wins until it is used",
    icon: "key",
    trust: "human",
    components: ["approvals"],
    parts: [],
    what: [
      "The provider drafts one fix for the best supported claim. The safety policy judges it from trusted configuration (the proposer's own risk label is ignored): destructive actions are always denied, and a reversible fix needs a person's approval (in a simulation, also the auto-approver when a run asks for it; on a real target, only a person who types the service's name).",
      "An approval is bound to the proposal's digest (action, target and parameters), expires and is used once. A rejection wins until the approval is used. Through the API and this dashboard, deciding needs the operator token.",
    ],
    onTarget: {
      simulated: "At the console y or yes approves (anything else rejects); in this dashboard, the Approve button.",
      kind: "Only the target service's exact name approves: typed at the console (anything else, y included, rejects) or in this dashboard's approval dialog (anything else decides nothing).",
      sregym: "As on Kind: only the target service's exact name approves, typed at the console or in this dashboard's approval dialog.",
    },
    modules: ["recap/safety/approvals.py", "recap/safety/policy.py", "apps/api/auth.py"],
    adrs: ["0005", "0015"],
    events: ["mitigation.proposed", "approval.requested", "approval.decided"],
    page: "safety",
    next: "the approved digest",
  },
  {
    id: "write",
    step: 9,
    title: "Authorized write",
    summary: "Executed through the gateway with a signed, single-use capability",
    icon: "lock",
    trust: "trusted",
    components: ["write_boundary"],
    parts: [],
    what: [
      "Only the controller executes the approved fix, through the gateway, which signs a capability (Ed25519, one key per run) for exactly that action. The environment's write boundary verifies it first and refuses a write without a valid, unused, unexpired capability.",
      "A write-ahead event is traced before the write is sent.",
    ],
    onTarget: {
      simulated: "The simulated shop's write boundary verifies the capability, then changes only its own state (a per-run SQLite file).",
      kind: "The Kubernetes adapter verifies the capability and its own rules, then writes as recap-writer: RBAC lets it patch only the two demo Deployments in its namespace, and an admission policy checks each patch's shape. A server-side dry run, the write, a read-back and an audit row (see How a real Kubernetes action is authorized, below).",
      sregym: "The same adapter and steps, as RECAP's own recap-writer in SREGym's cluster: RBAC lets it patch only the app's named Deployments in one namespace. SREGym's cluster has no admission policy, so RBAC is the only limit there. A rollback restores a revision's own image tag.",
    },
    modules: [
      "recap/safety/capabilities.py",
      "recap/tools/gateway.py",
      "recap/tools/actions.py",
      "recap/environments/kubernetes/engine.py",
      "infra/kind/manifests/",
      "infra/sregym/recap-rbac.yaml",
    ],
    adrs: ["0014", "0005", "0031"],
    events: ["mitigation.started", "mitigation.executed"],
    page: "safety",
    next: "health check (never an MCP tool)",
  },
  {
    id: "oracle",
    step: 10,
    title: "Recovery oracle",
    summary: "Judges recovery from the environment's own health, never from agent text",
    icon: "heart",
    trust: "trusted",
    components: ["oracle"],
    parts: [],
    what: [
      "After a fix, the oracle checks the environment's health on its own channel, never through an MCP tool, and compares before with after. It only ever says yes or no, and its findings never reach a provider.",
      "A run counts as resolved only when the oracle verified the recovery.",
    ],
    onTarget: {
      simulated: "Reads the simulated shop's noise-free health straight from the simulator's state, through a trusted call that is not an MCP tool.",
      kind: "As its own recap-oracle identity: a complete rollout, readiness, and its own HTTP probe of each service (not Prometheus, which the agents read), in two rounds a few seconds apart.",
      sregym: "No probe and no metrics there, so: a complete rollout, readiness, and containers that stayed up a minute with no exit in the last five. SREGym's own grader gives the benchmark's verdict separately; it is recorded by hand, never counted as RECAP's success.",
    },
    modules: ["recap/verification/oracle.py", "recap/environments/kubernetes/probe.py", "recap/environments/kubernetes/rollout.py"],
    adrs: ["0010", "0013", "0029", "0031"],
    events: ["verification.recovery"],
    page: "live",
    next: "final state",
  },
  {
    id: "episode",
    step: 11,
    title: "Episode (memory)",
    summary: "The finished run is remembered for the next incident",
    icon: "save",
    trust: "trusted",
    components: ["memory"],
    parts: [],
    what: [
      "A finished run is summarized by deterministic extraction into an experience episode in its own partition: DEMO for the scripted policy, LIVE for real models. Replays write none.",
      "An episode counts as a success only if the oracle verified the recovery. It feeds the next incident's memory search.",
    ],
    modules: ["recap/memory/consolidation.py", "recap/memory/store.py"],
    adrs: ["0023"],
    events: ["memory.consolidated", "memory.skip"],
    page: "memory",
    loop: "Feeds the next incident's memory search (step 3).",
  },
  {
    id: "environment",
    step: null,
    title: "Target environment",
    summary: "The adapter behind every alert, tool read, authorized write and health check",
    icon: "package",
    trust: "untrusted",
    components: ["environment"],
    parts: [],
    what: [
      "All infrastructure sits behind one TargetEnvironment adapter, chosen by configuration: the deterministic simulated shop by default (configs/environment.yaml), or a real Kubernetes cluster through a target profile (RECAP_TARGET=kind-demo or sregym-hotel, configs/targets/). The controller, gateway, retrieval, memory, router and agents keep their code.",
      "Each server runs every incident on its one configured adapter. GET /system describes it without contacting a cluster; the Overview's Check now asks the server to reach it.",
    ],
    onTarget: {
      simulated: "A deterministic, in-process simulation of a five-service shop, its state in a per-run SQLite file. No real system is involved.",
      kind: "A Kind cluster RECAP's owner controls, with two demo services, reached only through typed kubectl operations as four RBAC-limited identities (reader, writer, oracle, scenario), with Prometheus metrics and an HTTP probe of the oracle's own.",
      sregym: "SREGym 1.1's own Kind cluster, whose harness deploys a benchmark application and injects the problem. RECAP works through three identities of its own (reader, writer, oracle), with no probe or metrics there, and rolls back by image tag.",
    },
    modules: [
      "recap/core/interfaces.py",
      "recap/environments/registry.py",
      "recap/environments/simulated/",
      "recap/environments/kubernetes/",
      "integrations/sregym/",
      "configs/environment.yaml",
      "configs/targets/",
      "infra/kind/",
      "infra/sregym/",
    ],
    adrs: ["0006", "0007", "0031"],
    events: [],
    page: null,
  },
  {
    id: "telemetry",
    step: null,
    title: "Trace & recordings",
    summary: "Every step is a structured trace event; every model exchange is recorded",
    icon: "timeline",
    trust: "trusted",
    components: ["trace_sinks", "opentelemetry"],
    parts: [
      { label: "Trace sinks", component: "trace_sinks" },
      { label: "OpenTelemetry", component: "opentelemetry" },
    ],
    what: [
      "Every step above emits ordered, redacted trace events, stored in SQLite and JSONL and streamed live to this dashboard. Every model exchange is recorded without keys, so a real-model run can be replayed offline.",
      "The API serves typed, read-only views built only from what a run persisted, and names every field it did not record.",
    ],
    modules: ["recap/telemetry/tracer.py", "recap/telemetry/sinks.py", "recap/providers/recordings.py", "apps/api/views.py"],
    adrs: ["0002", "0018", "0027", "0028", "0030"],
    events: ["llm.call", "run.summary"],
    page: "timeline",
  },
];

/** The eleven steps of the flow, in order. */
export const FLOW_STEPS: readonly ArchNode[] = ARCH_NODES.filter((node) => node.step !== null);

/** The nodes around the flow (target environment, telemetry). */
export const AROUND_NODES: readonly ArchNode[] = ARCH_NODES.filter((node) => node.step === null);

/** Keyboard order of the nodes: the flow, then the nodes around it. */
export const NODE_ORDER: readonly ArchNodeId[] = [...FLOW_STEPS, ...AROUND_NODES].map((node) => node.id);

export function archNode(id: string | null | undefined): ArchNode | null {
  return ARCH_NODES.find((node) => node.id === id) ?? null;
}

/** The node a deep link names (`?node=`), or the first step. */
export function selectedNodeId(param: string | null | undefined): ArchNodeId {
  return archNode(param)?.id ?? "alert";
}

/**
 * The node a key moves to in the node list (a vertical tab list): arrows move by one and
 * wrap, Home and End jump to the ends. Null: the key does not move the selection.
 */
export function nextNodeId(current: ArchNodeId, key: string): ArchNodeId | null {
  const index = NODE_ORDER.indexOf(current);
  const last = NODE_ORDER.length - 1;
  switch (key) {
    case "ArrowDown":
    case "ArrowRight":
      return NODE_ORDER[index >= last ? 0 : index + 1];
    case "ArrowUp":
    case "ArrowLeft":
      return NODE_ORDER[index <= 0 ? last : index - 1];
    case "Home":
      return NODE_ORDER[0];
    case "End":
      return NODE_ORDER[last];
    default:
      return null;
  }
}

/** A node's status: its first /system component's, or null when /system lists none for it. */
export function nodeStatus(node: ArchNode, system: Pick<SystemView, "components"> | null | undefined): { component: ComponentStatus | null; look: StatusLook } | null {
  if (!node.components.length) return null;
  const component = componentById(system, node.components[0]);
  return { component, look: component ? componentLook(component.status) : componentLook(null) };
}

/** Server facts shown for a node (label, value), read from GET /system. */
export function nodeFacts(node: ArchNode, system: SystemView): [string, string][] {
  const f = system.features;
  const v = system.versions;
  const yesNo = (value: boolean) => (value ? "yes" : "no");
  switch (node.id) {
    case "alert":
      return [
        ["Environment", `${system.environment.name} (kind ${system.environment.kind})`],
        ["Simulated", yesNo(system.environment.is_simulated)],
        ["Actions it supports", system.environment.supported_actions.join(", ") || "none listed"],
      ];
    case "controller":
      return [
        ["RECAP version", v.recap],
        ["Trace schema", v.trace_schema],
        ["Configuration hash", v.config_hash],
      ];
    case "retrieval":
      return [
        ["Default search", f.retrieval_default],
        ["Hybrid search can run", yesNo(f.hybrid_ready)],
        ["Stored vectors", system.embedder ?? "Not recorded"],
        ["Runbook chunks", String(f.knowledge_chunks)],
        ["Agent searches per run", f.agent_search.max_per_run !== undefined ? String(f.agent_search.max_per_run) : "Not recorded"],
        ["Episodes in memory", String(system.memory_episodes)],
      ];
    case "routing":
      return [["Router", v.router]];
    case "specialists": {
      const provider = system.provider as { name?: unknown; origin?: unknown };
      return [
        ["Default provider", typeof provider.name === "string" ? provider.name : "Not recorded"],
        ["Providers a run may pick", f.per_run_providers.join(", ") || "none"],
        ["Prompt version (real models)", v.prompt_version],
      ];
    }
    case "gateway":
      return [
        ["Tool transport", system.tool_transport],
        ["Safety policy", system.safety_policy],
      ];
    case "verifier":
      return [["Verifier", v.verifier]];
    case "approval":
      return [
        ["Deciding needs the operator token", yesNo(f.writes_require_operator_token)],
        // POST /approvals/{id} asks for it exactly when the target is not simulated.
        ["Approving needs the service's name typed", system.environment.is_simulated ? "no (simulated target)" : "yes (not simulated)"],
      ];
    case "write": {
      const boundary = componentById(system, "write_boundary");
      return [
        ["Write boundary", boundary ? boundary.implementation : "Not reported"],
        ["Actions it supports", system.environment.supported_actions.join(", ") || "none listed"],
      ];
    }
    case "oracle":
      return [["Oracle", system.oracle]];
    case "episode":
      return [
        ["Partitions", f.memory_partitions.join(", ")],
        ...Object.entries(f.memory_access).map(([origin, rule]): [string, string] => [
          SOURCE_LABELS[origin as SourceKey] ?? origin,
          `reads ${rule.read.join(", ") || "nothing"} · writes ${rule.write ?? "nothing"}`,
        ]),
      ];
    case "environment":
      return [
        ["Configured kind", system.environment_kind],
        ["Adapter class", configuredAdapter(system)?.adapter ?? "Not reported"],
        ["Namespace", system.environment.namespace],
        ["Simulated", yesNo(system.environment.is_simulated)],
        ["Safety policy version", v.policy_version],
      ];
    case "telemetry":
      return [
        ["Trace sinks", system.trace_sinks.join(", ")],
        ["OpenTelemetry", f.otel ? "on" : "off"],
        ["API schema", v.api_schema],
      ];
  }
}

// ======================================================================================
// How a real Kubernetes action is authorized (Architecture & Learning)
// ======================================================================================

export interface AuthorizationStep {
  title: string;
  /** One or two plain sentences (RECAP's documentation of its own code). */
  text: string;
  /** Repository paths that implement it (folders end with "/"). */
  links: string[];
}

/**
 * What a write to a real cluster (the Kind demo or SREGym's) passes, in order, from the
 * model's proposal to the oracle's verdict: the API and console approvals, the capability,
 * KubernetesEnvironment.execute_authorized and the manifests that bound its writer.
 */
export const KUBERNETES_AUTHORIZATION: readonly AuthorizationStep[] = [
  {
    title: "One exact proposal",
    text: "A model proposes one action on one service: a rollback to a named revision or a rolling restart. The safety policy judges it from trusted configuration, and a reversible write needs a person.",
    links: ["recap/safety/policy.py"],
  },
  {
    title: "The verifier's checks",
    text: "Rules VP1 to VP4: the fix cites real evidence, targets the diagnosed service, names an action, and restores only a version the evidence names.",
    links: ["recap/agents/verifier.py"],
  },
  {
    title: "A person types the service's name",
    text: "The approval is bound to the proposal's digest, expires and is used once. The operator types the target service's exact name, at the console or in this dashboard; a rejection wins until the approval is used.",
    links: ["recap/safety/approvals.py"],
  },
  {
    title: "A signed, single-use capability",
    text: "The gateway signs an Ed25519 capability for exactly that action, service and parameters. The adapter verifies it and commits its nonce before touching the cluster, so it never works twice; it refuses any other action, and every write while its write switch is on.",
    links: ["recap/safety/capabilities.py", "recap/environments/kubernetes/engine.py"],
  },
  {
    title: "The writer's limits",
    text: "The write goes out as recap-writer, a ServiceAccount whose RBAC lets it read and patch only the app's named Deployments in one namespace (and list their ReplicaSets, to find a revision). On the Kind demo an admission policy also checks each patch's shape; SREGym's cluster has RBAC only.",
    links: ["infra/kind/manifests/", "infra/sregym/recap-rbac.yaml"],
  },
  {
    title: "Dry run, write, read-back, audit",
    text: "A server-side dry run first, then the write, a read-back of the Deployment, and an audit row for the run, whatever the outcome.",
    links: ["recap/environments/kubernetes/engine.py"],
  },
  {
    title: "The independent oracle",
    text: "Only RECAP's oracle, reading the cluster as its own identity, says whether the service recovered. On SREGym, SREGym's own grader gives the benchmark's verdict separately.",
    links: ["recap/verification/oracle.py", "recap/environments/kubernetes/probe.py"],
  },
];
